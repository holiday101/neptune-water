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
    joined = gpd.sjoin(points_gdf, parcels_gdf[["parcel_id", "geometry"]], how="left", predicate="within")
    geocode_matches = {
        row["meter_id"]: row["parcel_id"]
        for _, row in joined.iterrows() if pd.notna(row["parcel_id"])
    }
    print(f"  {len(geocode_matches)} / {len(geocoded)} geocoded points fell inside a parcel")

    print("Falling back to address matching for the rest...")
    exact_idx, prefix_idx = build_address_index(parcels_gdf)
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


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--backfill-areas":
        backfill_areas()
    elif len(sys.argv) == 2:
        run(sys.argv[1])
    else:
        print("Usage: python3 import_gis.py <path to parcels .geojson>")
        print("       python3 import_gis.py --backfill-areas   (fixup for a pre-existing db)")
        sys.exit(1)
