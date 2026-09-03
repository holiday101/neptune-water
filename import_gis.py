"""Loads county parcel boundary GeoJSON, geocodes billing addresses via the
free Census Bureau batch geocoder, and joins each meter to a parcel via
point-in-polygon — falling back to normalized address-string matching only
for whatever the geocoder can't resolve.

Why geocode instead of just string-matching addresses: a coordinate landing
inside a specific polygon is much stronger evidence than two address strings
looking similar, and it sidesteps formatting differences between the billing
system's addresses and the county's (missing directional prefixes, missing
street-type suffixes, abbreviations) entirely.

Usage:
    python3 import_gis.py "/path/to/parcels.geojson"

Re-run any time you have a fresh parcel export or billing import — both
target tables are fully replaced each run (small enough data that an upsert
isn't worth the complexity, and stale matches from a prior parcel vintage
shouldn't linger).
"""
import csv
import io
import json
import re
import sys
from datetime import datetime, timezone

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import shape

import neptune_db as db

CENSUS_BATCH_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
CENSUS_BATCH_LIMIT = 10_000  # Census caps a single batch submission at this

# Parcel geometry is stored/joined in WGS84 (EPSG:4326), whose coordinates
# are degrees — .area on that is meaningless as a physical size. To get real
# square footage/acreage we reproject into a local metric CRS just for the
# area calculation. UTM zone 12N (EPSG:32612) covers roughly -114 to -108
# longitude, which is where this utility's county sits (parcel longitudes
# run around -111.7 to -111.9) — distortion at that scale is a few cm per
# km, far below what matters for parcel-sized acreage. If you ever import a
# different county far outside that longitude band, pick the matching UTM
# zone instead (or any other equal-area/local projected CRS).
AREA_CRS = "EPSG:32612"
SQM_PER_ACRE = 4046.8564224
SQFT_PER_SQM = 10.7639104167

# This utility's addresses are overwhelmingly in one city (verified against
# the parcel data: ~1700 of ~2000 matched addresses were "Providence") — used
# as the default city/state/zip fed to the geocoder alongside each street
# address. Addresses actually in a neighboring city usually still geocode
# fine off the street name alone; a wrong ZIP rarely blocks a match outright.
DEFAULT_CITY = "Providence"
DEFAULT_STATE = "UT"
DEFAULT_ZIP = "84332"

DIRECTIONS = {"N": "NORTH", "S": "SOUTH", "E": "EAST", "W": "WEST",
              "NE": "NORTHEAST", "NW": "NORTHWEST", "SE": "SOUTHEAST", "SW": "SOUTHWEST"}
STREET_TYPES = {
    "ST": "STREET", "AVE": "AVENUE", "DR": "DRIVE", "RD": "ROAD", "LN": "LANE",
    "CT": "COURT", "BLVD": "BOULEVARD", "CIR": "CIRCLE", "PL": "PLACE",
    "HWY": "HIGHWAY", "PKWY": "PARKWAY", "HTS": "HEIGHTS", "TER": "TERRACE",
}


def _normalize_addr(addr):
    if not addr:
        return None
    s = re.sub(r"[.,#]", "", str(addr).upper()).strip()
    s = re.sub(r"\s+", " ", s)
    return " ".join(DIRECTIONS.get(t, STREET_TYPES.get(t, t)) for t in s.split(" "))


def load_parcels(path):
    with open(path) as f:
        data = json.load(f)
    rows, geoms = [], []
    for feat in data["features"]:
        p = feat["properties"]
        pid = p.get("PARCEL_ID")
        if not pid or not feat.get("geometry"):
            continue
        rows.append({"parcel_id": pid, "address": p.get("PARCEL_ADD"), "city": p.get("PARCEL_CITY")})
        geoms.append(shape(feat["geometry"]))
    gdf = gpd.GeoDataFrame(rows, geometry=geoms, crs="EPSG:4326")
    # Some parcel IDs legitimately cover multiple polygon features (e.g. a
    # parcel split across a multipart shape isn't uncommon in county data);
    # dissolve to one row per parcel_id so the point-in-polygon join and the
    # parcels table both have a clean 1:1 relationship with parcel_id.
    gdf = gdf.dissolve(by="parcel_id", aggfunc="first").reset_index()

    # Area in a local metric CRS (see AREA_CRS above), then converted — the
    # gdf itself stays in EPSG:4326 throughout (that's what the sjoin against
    # geocoded points and the stored geometry column both expect).
    area_sqm = gdf.geometry.to_crs(AREA_CRS).area
    gdf["area_sqft"] = area_sqm * SQFT_PER_SQM
    gdf["area_acres"] = area_sqm / SQM_PER_ACRE
    return gdf


def geocode_addresses(pairs):
    """pairs: list of (meter_id, location). Returns dict
    meter_id -> (lon, lat) for everything the geocoder matched."""
    results = {}
    for i in range(0, len(pairs), CENSUS_BATCH_LIMIT):
        chunk = pairs[i:i + CENSUS_BATCH_LIMIT]
        buf = io.StringIO()
        writer = csv.writer(buf)
        for meter_id, loc in chunk:
            writer.writerow([meter_id, loc, DEFAULT_CITY, DEFAULT_STATE, DEFAULT_ZIP])

        resp = requests.post(
            CENSUS_BATCH_URL,
            files={"addressFile": ("addresses.csv", buf.getvalue(), "text/csv")},
            data={"benchmark": "Public_AR_Current"},
            timeout=300,
        )
        resp.raise_for_status()

        for row in csv.reader(io.StringIO(resp.text)):
            if len(row) >= 6 and row[2] == "Match":
                lon, lat = row[5].split(",")
                results[row[0]] = (float(lon), float(lat))
    return results


def addressed_parcels_gdf(conn):
    """GeoDataFrame of only the parcels that have an address. In this
    county's parcel export, a parcel with no address is street / public
    right-of-way, not a taxable lot — i.e. a road (see also the README).
    Roads are never a valid match target for a meter: used everywhere a
    meter gets matched to a parcel, by point-in-polygon or by nearest
    distance, so a meter can never land on one (see match_points_to_parcels
    below and its callers in this file and import_meter_locations.py)."""
    rows = conn.execute("SELECT parcel_id, geometry FROM parcels WHERE address IS NOT NULL").fetchall()
    return gpd.GeoDataFrame(
        {"parcel_id": [r["parcel_id"] for r in rows]},
        geometry=[shape(json.loads(r["geometry"])) for r in rows],
        crs="EPSG:4326",
    )


def match_points_to_parcels(points_gdf, addressed_gdf):
    """Matches each point in points_gdf (EPSG:4326) to the closest parcel
    in addressed_gdf — which the caller must have already filtered to
    non-road parcels (see addressed_parcels_gdf). Point-in-polygon first;
    for anything that misses (either a point genuinely outside every
    parcel, or — very commonly — a point that lands inside a road polygon,
    which was excluded and so can never itself be the match), falls back to
    whichever addressed parcel is nearest.

    Returns a DataFrame aligned to points_gdf's index with columns
    parcel_id, match_method ('within' | 'nearest' | None), match_dist_m
    (0.0 for 'within', the real distance in meters for 'nearest')."""
    out = pd.DataFrame(
        {"parcel_id": pd.NA, "match_method": pd.NA, "match_dist_m": pd.NA},
        index=points_gdf.index,
    )
    if addressed_gdf.empty:
        return out

    within = gpd.sjoin(points_gdf[["geometry"]], addressed_gdf, how="left", predicate="within")
    within = within[~within.index.duplicated(keep="first")]
    out["parcel_id"] = within["parcel_id"]
    matched = out["parcel_id"].notna()
    out.loc[matched, "match_method"] = "within"
    out.loc[matched, "match_dist_m"] = 0.0

    unmatched = out["parcel_id"].isna()
    if unmatched.any():
        pts_m = points_gdf.loc[unmatched, ["geometry"]].to_crs(AREA_CRS)
        addressed_m = addressed_gdf.to_crs(AREA_CRS)
        nearest = gpd.sjoin_nearest(
            pts_m, addressed_m[["parcel_id", "geometry"]], distance_col="dist_m"
        )
        nearest = nearest[~nearest.index.duplicated(keep="first")]
        out.loc[nearest.index, "parcel_id"] = nearest["parcel_id"].values
        out.loc[nearest.index, "match_method"] = "nearest"
        out.loc[nearest.index, "match_dist_m"] = nearest["dist_m"].values

    return out


def build_address_index(parcels_gdf):
    """Normalized-address -> set of parcel_ids, for the string-match fallback."""
    exact, prefix = {}, {}
    for _, row in parcels_gdf.iterrows():
        norm = _normalize_addr(row["address"])
        if not norm:
            continue
        exact.setdefault(norm, set()).add(row["parcel_id"])
        parts = norm.split(" ")
        if len(parts) >= 3:
            prefix.setdefault(" ".join(parts[:-1]), set()).add(row["parcel_id"])
    return exact, prefix


def run(geojson_path, conn=None):
    conn = conn or db.get_conn()
    now = datetime.now(timezone.utc).isoformat()

    print("Loading parcels...")
    parcels_gdf = load_parcels(geojson_path)
    print(f"  {len(parcels_gdf)} parcels")

    billing = pd.read_sql_query(
        "SELECT meter_id, location FROM customer_billing WHERE location IS NOT NULL",
        conn,
    )
    print(f"Geocoding {len(billing)} billing addresses via Census batch geocoder...")
    geocoded = geocode_addresses(list(billing.itertuples(index=False, name=None)))
    print(f"  {len(geocoded)} / {len(billing)} geocoded")

    points_gdf = gpd.GeoDataFrame(
        {"meter_id": list(geocoded.keys())},
        geometry=gpd.points_from_xy(
            [v[0] for v in geocoded.values()], [v[1] for v in geocoded.values()]
        ),
        crs="EPSG:4326",
    )
    # Matched against addressed (non-road) parcels only — a geocoded point
    # landing inside a road/right-of-way polygon is nudged to the nearest
    # real parcel instead of being "matched" to the road itself (see
    # addressed_parcels_gdf / match_points_to_parcels).
    match = match_points_to_parcels(points_gdf, addressed_parcels_gdf(conn))
    geocode_matches = {
        points_gdf.loc[idx, "meter_id"]: row["parcel_id"]
        for idx, row in match.iterrows() if pd.notna(row["parcel_id"])
    }
    n_within = int((match["match_method"] == "within").sum())
    n_nearest = int((match["match_method"] == "nearest").sum())
    print(
        f"  {len(geocode_matches)} / {len(geocoded)} geocoded points matched a non-road parcel "
        f"({n_within} directly inside one, {n_nearest} nudged to the nearest one because the "
        "point landed on a road)"
    )

    print("Falling back to address matching for the rest...")
    # Scoped to DEFAULT_CITY, not the full parcels_gdf — this geojson covers
    # the whole county (19 cities), and small Utah towns share a lot of grid
    # addresses ("125 W CENTER ST", "150 W 200 S", ...). An address that
    # doesn't exist under this exact normalized string in Providence's own
    # data (which is exactly when this fallback runs) can still happen to be
    # unique somewhere else in the county — found 21 meters this way matched
    # to parcels in Logan/Smithfield/Mendon/etc. instead of going unmatched.
    # The geocode step above isn't scoped like this because it's matching a
    # real lat/lon against physical polygons, not a coincidental string hit.
    providence_gdf = parcels_gdf[parcels_gdf["city"] == DEFAULT_CITY]
    exact_idx, prefix_idx = build_address_index(providence_gdf)
    fallback_matches, fallback_method = {}, {}
    for meter_id, loc in billing.itertuples(index=False, name=None):
        if meter_id in geocode_matches:
            continue
        norm = _normalize_addr(loc)
        if norm in exact_idx and len(exact_idx[norm]) == 1:
            fallback_matches[meter_id] = next(iter(exact_idx[norm]))
            fallback_method[meter_id] = "address_exact"
        elif norm in prefix_idx and len(prefix_idx[norm]) == 1:
            fallback_matches[meter_id] = next(iter(prefix_idx[norm]))
            fallback_method[meter_id] = "address_prefix"
    print(f"  {len(fallback_matches)} more matched via address string")

    rows = []
    for meter_id in billing["meter_id"]:
        if meter_id in geocode_matches:
            lon, lat = geocoded[meter_id]
            rows.append({
                "meter_id": meter_id, "parcel_id": geocode_matches[meter_id],
                "match_method": "geocoded", "lat": lat, "lon": lon, "matched_at": now,
            })
        elif meter_id in fallback_matches:
            rows.append({
                "meter_id": meter_id, "parcel_id": fallback_matches[meter_id],
                "match_method": fallback_method[meter_id], "lat": None, "lon": None, "matched_at": now,
            })

    total = len(billing)
    matched = len(rows)
    print(f"\nTotal: {matched} / {total} meters matched to a parcel ({matched/total:.1%})")

    parcel_features = json.loads(parcels_gdf.to_json())["features"]
    parcel_records = [
        {
            "parcel_id": feat["properties"]["parcel_id"],
            "address": feat["properties"]["address"],
            "city": feat["properties"]["city"],
            "geometry": json.dumps(feat["geometry"]),
            "area_sqft": feat["properties"]["area_sqft"],
            "area_acres": feat["properties"]["area_acres"],
        }
        for feat in parcel_features
    ]
    db.replace_parcels(conn, parcel_records)
    db.replace_meter_parcels(conn, rows)
    return {"total": total, "matched": matched}


def backfill_areas(conn=None):
    """One-off (or re-runnable) fixup for parcels imported before
    area_sqft/area_acres existed: recomputes area straight from each row's
    already-stored geometry, so it doesn't need the original .geojson file
    or a re-run of the (rate-limited) Census geocoding step. `run()` above
    computes these fresh on every normal import, so this is only needed
    once per pre-existing database."""
    conn = conn or db.get_conn()
    parcel_rows = conn.execute("SELECT parcel_id, geometry FROM parcels").fetchall()
    print(f"Computing area for {len(parcel_rows)} parcels...")

    gdf = gpd.GeoDataFrame(
        {"parcel_id": [r["parcel_id"] for r in parcel_rows]},
        geometry=[shape(json.loads(r["geometry"])) for r in parcel_rows],
        crs="EPSG:4326",
    )
    area_sqm = gdf.geometry.to_crs(AREA_CRS).area
    updates = [
        {"parcel_id": pid, "area_sqft": sqm * SQFT_PER_SQM, "area_acres": sqm / SQM_PER_ACRE}
        for pid, sqm in zip(gdf["parcel_id"], area_sqm)
    ]
    conn.executemany(
        "UPDATE parcels SET area_sqft = :area_sqft, area_acres = :area_acres WHERE parcel_id = :parcel_id",
        updates,
    )
    conn.commit()
    print(f"  done — {len(updates)} parcels updated")
    return {"updated": len(updates)}


def fix_road_matches(conn=None):
    """One-off (or re-runnable) repair for meter_parcels / gis_meters rows
    that got matched to a road (a parcel with no address) before run() /
    import_meter_locations.py started excluding those — see
    addressed_parcels_gdf and match_points_to_parcels. Re-resolves each
    affected row to the nearest addressed parcel using its already-stored
    lat/lon, so it needs neither the original parcels .geojson nor the
    meter shapefile, nor a re-run of the rate-limited Census geocoder —
    just what's already in the database.

    Handles all three affected sources:
      - gis_meters: every row has a surveyed lat/lon, so every road match
        there is directly fixable.
      - meter_parcels rows with match_method='geocoded': these carry their
        own lat/lon from the geocoding step, so they're directly fixable too.
      - meter_parcels rows with match_method='gis_survey': copied over from
        gis_meters by import_meter_locations.match_customers_via_survey(),
        including its lat/lon — so once gis_meters is fixed above,
        re-running that same reconciliation cascades the fix into most of
        these for free. A few can be left stale by that reconciliation's
        own ambiguity rule (skips an address whose surveyed meters now span
        more than one parcel) — those still have their own copied lat/lon
        in meter_parcels, so a final direct pass mops them up too.
    match_method='address_exact'/'address_prefix' rows are never affected:
    that fallback only ever indexes parcels that have an address, i.e.
    already excludes roads.
    """
    conn = conn or db.get_conn()
    addressed_gdf = addressed_parcels_gdf(conn)
    if addressed_gdf.empty:
        print("No addressed parcels found — nothing to fix against.")
        return {"gis_meters_fixed": 0, "meter_parcels_geocoded_fixed": 0}

    def _refix(rows, table, extra_set_cols=()):
        if not rows:
            return 0
        pts = gpd.GeoDataFrame(
            {"meter_id": [r["meter_id"] for r in rows]},
            geometry=gpd.points_from_xy([r["lon"] for r in rows], [r["lat"] for r in rows]),
            crs="EPSG:4326",
        )
        match = match_points_to_parcels(pts, addressed_gdf)
        updates = []
        for idx, row in match.iterrows():
            if pd.isna(row["parcel_id"]):
                continue
            update = {"meter_id": pts.loc[idx, "meter_id"], "parcel_id": row["parcel_id"]}
            for col in extra_set_cols:
                update[col] = row[col]
            updates.append(update)
        if updates:
            set_clause = ", ".join(f"{c} = :{c}" for c in ("parcel_id", *extra_set_cols))
            conn.executemany(f"UPDATE {table} SET {set_clause} WHERE meter_id = :meter_id", updates)
        return len(updates)

    road_gis = conn.execute("""
        SELECT g.meter_id, g.lat, g.lon FROM gis_meters g
        JOIN parcels p ON p.parcel_id = g.parcel_id
        WHERE p.address IS NULL
    """).fetchall()
    gis_fixed = _refix(road_gis, "gis_meters", extra_set_cols=("match_method", "match_dist_m"))
    print(f"  gis_meters: re-matched {gis_fixed} / {len(road_gis)} road-matched meters to a non-road parcel")

    road_geocoded = conn.execute("""
        SELECT mp.meter_id, mp.lat, mp.lon FROM meter_parcels mp
        JOIN parcels p ON p.parcel_id = mp.parcel_id
        WHERE p.address IS NULL AND mp.match_method = 'geocoded' AND mp.lat IS NOT NULL
    """).fetchall()
    geocoded_fixed = _refix(road_geocoded, "meter_parcels")
    print(f"  meter_parcels (geocoded): re-matched {geocoded_fixed} / {len(road_geocoded)}")
    conn.commit()

    # gis_survey rows in meter_parcels are copied from gis_meters, so
    # re-running that reconciliation now that gis_meters is fixed cascades
    # the fix into them too, without needing to touch them directly.
    import import_meter_locations as iml
    print("  meter_parcels (gis_survey): re-reconciling against the fixed GIS survey...")
    reconcile = iml.match_customers_via_survey(conn)
    conn.commit()
    print(f"    {reconcile['changed']} rows changed")

    # Mop-up: a handful of gis_survey rows can be left stale by that
    # reconciliation's own ambiguity rule (it skips an address whose
    # surveyed meters now span more than one parcel) — those still have
    # their own copied lat/lon in meter_parcels, so fix them directly too.
    road_leftover = conn.execute("""
        SELECT mp.meter_id, mp.lat, mp.lon FROM meter_parcels mp
        JOIN parcels p ON p.parcel_id = mp.parcel_id
        WHERE p.address IS NULL AND mp.lat IS NOT NULL
    """).fetchall()
    leftover_fixed = _refix(road_leftover, "meter_parcels")
    if road_leftover:
        print(f"  meter_parcels (leftover): re-matched {leftover_fixed} / {len(road_leftover)}")
        conn.commit()

    remaining = conn.execute("""
        SELECT COUNT(*) n FROM meter_parcels mp JOIN parcels p ON p.parcel_id = mp.parcel_id
        WHERE p.address IS NULL
    """).fetchone()["n"]
    remaining += conn.execute("""
        SELECT COUNT(*) n FROM gis_meters g JOIN parcels p ON p.parcel_id = g.parcel_id
        WHERE p.address IS NULL
    """).fetchone()["n"]
    if remaining:
        print(f"  WARNING: {remaining} road match(es) remain — likely rows with no stored "
              "lat/lon (address_exact/address_prefix shouldn't produce these; investigate if seen).")
    else:
        print("  0 road matches remaining.")

    return {
        "gis_meters_fixed": gis_fixed, "meter_parcels_geocoded_fixed": geocoded_fixed,
        "meter_parcels_leftover_fixed": leftover_fixed, "remaining_road_matches": remaining,
        **reconcile,
    }


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--backfill-areas":
        backfill_areas()
    elif len(sys.argv) == 2 and sys.argv[1] == "--fix-roads":
        fix_road_matches()
    elif len(sys.argv) == 2:
        run(sys.argv[1])
    else:
        print("Usage: python3 import_gis.py <path to parcels .geojson>")
        print("       python3 import_gis.py --backfill-areas   (fixup for a pre-existing db)")
        print("       python3 import_gis.py --fix-roads        (re-match meters currently on a road)")
        sys.exit(1)
