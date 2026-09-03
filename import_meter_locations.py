"""Loads the utility's meter-inventory GIS export (a shapefile of surveyed
meter locations — NOT from Neptune, and not the same ID space as
customers.miu_id/customer_billing.meter_id, see gis_meters in neptune_db.py)
and attaches a parcel_id to each meter via point-in-polygon against the
`parcels` table already loaded by import_gis.py.

Why this needs no geocoding, unlike import_gis.py's billing-address matching:
these coordinates come from an actual GPS survey of each meter, not a
geocoded street address — so the point-in-polygon join is direct and should
be both more complete and more confident than the billing-address path.

Usage:
    python3 import_meter_locations.py "/path/to/WATER_Meter.zip"
    python3 import_meter_locations.py "/path/to/WATER_Meter.shp"

Requires parcels to already be loaded (run import_gis.py first) — otherwise
every meter comes back with parcel_id = NULL and a warning is printed.

Re-run any time you get a fresh meter-inventory export — gis_meters is fully
replaced each run, same reasoning as parcels/meter_parcels in import_gis.py.
"""
import json
import os
import sys
import tempfile
import zipfile
from datetime import datetime, timezone

import geopandas as gpd
import pandas as pd
from shapely.geometry import shape

import neptune_db as db

# Parcel geometry (like everywhere else in this codebase) is stored in WGS84
# (EPSG:4326), whose coordinates are degrees — not usable for a real-world
# distance threshold. AREA_CRS from import_gis.py (UTM zone 12N) covers this
# same county, so reuse it here for the nearest-parcel fallback below.
DIST_CRS = "EPSG:32612"

# Meters are typically installed at the curb/property line, not inside the
# parcel polygon itself, so a strict point-in-polygon join misses a real
# chunk of otherwise-good GPS points. Checked against this data: every
# unmatched point after the strict join was within 12.7m of a parcel (median
# 3.3m) — so a small buffer recovers those without risking a false match
# onto some unrelated, merely-nearby parcel across a street.
NEAREST_FALLBACK_MAX_M = 20


def _read_shapefile(path):
    """Accepts a .zip (extracted to a temp dir) or a direct .shp path."""
    if path.lower().endswith(".zip"):
        with tempfile.TemporaryDirectory() as tmp:
            with zipfile.ZipFile(path) as zf:
                zf.extractall(tmp)
            shp_name = next((f for f in os.listdir(tmp) if f.lower().endswith(".shp")), None)
            if not shp_name:
                raise ValueError(f"No .shp file found inside {path}")
            return gpd.read_file(os.path.join(tmp, shp_name))
    return gpd.read_file(path)


def load_meters(path):
    """Returns a DataFrame of valid meter records: has a MeterID and a real
    (non-zero) surveyed lat/lon. ~370 of ~2,930 raw rows are dropped here —
    mostly 'EMPTY BARREL' placeholder rows with no MeterID and null-island
    (0, 0) coordinates, i.e. locations that were never actually surveyed."""
    gdf = _read_shapefile(path)

    df = pd.DataFrame({
        "meter_id": gdf["MeterID"].astype("string"),
        "lat": gdf["Lat"],
        "lon": gdf["Long"],
        "address": gdf["LocationAd"],
        "customer_name": gdf["CustomerNa"],
        "meter_size": gdf["MeterSize"],
        "service_type": gdf["Service_Ty"],
        "install_year": pd.to_numeric(gdf["InstallYea"], errors="coerce"),
        "edit_date": gdf["EditDate"],
    })
    df = df[df["meter_id"].notna() & (df["lat"] != 0) & (df["lon"] != 0)]

    # A handful of MeterIDs appear more than once (re-surveys / duplicate
    # field entries at the same address) — keep the most recently edited row
    # for each so a stale duplicate doesn't win arbitrarily.
    df = df.sort_values("edit_date").drop_duplicates("meter_id", keep="last")
    return df.drop(columns="edit_date")


def match_parcels(meters_df, conn):
    """Point-in-polygon join of each meter's surveyed lat/lon against the
    parcels table, with a small-buffer nearest-parcel fallback for meters
    that sit just outside every polygon (curb-side installs — see
    NEAREST_FALLBACK_MAX_M above). Returns meters_df with parcel_id,
    match_method ('within' or 'nearest'), and match_dist_m added (0.0 for
    'within', the actual distance for 'nearest', NaN/None for no match)."""
    parcel_rows = conn.execute("SELECT parcel_id, geometry FROM parcels").fetchall()
    if not parcel_rows:
        print("  WARNING: parcels table is empty — run import_gis.py first. "
              "All meters will be stored with parcel_id = NULL.")
        return meters_df.assign(parcel_id=pd.NA, match_method=pd.NA, match_dist_m=pd.NA)

    parcels_gdf = gpd.GeoDataFrame(
        {"parcel_id": [r["parcel_id"] for r in parcel_rows]},
        geometry=[shape(json.loads(r["geometry"])) for r in parcel_rows],
        crs="EPSG:4326",
    )
    points_gdf = gpd.GeoDataFrame(
        meters_df,
        geometry=gpd.points_from_xy(meters_df["lon"], meters_df["lat"]),
        crs="EPSG:4326",
    )

    within = gpd.sjoin(points_gdf, parcels_gdf, how="left", predicate="within")
    # A point exactly on a shared boundary can match more than one polygon;
    # sjoin duplicates the row in that case — keep just the first match.
    within = within[~within.index.duplicated(keep="first")]

    result = meters_df.assign(
        parcel_id=within["parcel_id"].values,
        match_method=pd.Series(within["parcel_id"].values).notna().map({True: "within", False: None}).values,
        match_dist_m=within["parcel_id"].notna().astype(float).values * 0.0,
    )
    result.loc[result["parcel_id"].isna(), "match_dist_m"] = pd.NA

    unmatched_mask = result["parcel_id"].isna()
    if unmatched_mask.any():
        parcels_m = parcels_gdf.to_crs(DIST_CRS)
        unmatched_pts = gpd.GeoDataFrame(
            result.loc[unmatched_mask],
            geometry=points_gdf.loc[unmatched_mask, "geometry"].values,
            crs="EPSG:4326",
        ).to_crs(DIST_CRS)

        nearest = gpd.sjoin_nearest(
            unmatched_pts[["geometry"]], parcels_m[["parcel_id", "geometry"]],
            distance_col="dist_m",
        )
        nearest = nearest[~nearest.index.duplicated(keep="first")]
        close_enough = nearest[nearest["dist_m"] <= NEAREST_FALLBACK_MAX_M]

        result.loc[close_enough.index, "parcel_id"] = close_enough["parcel_id"].values
        result.loc[close_enough.index, "match_method"] = "nearest"
        result.loc[close_enough.index, "match_dist_m"] = close_enough["dist_m"].values

    return result


def run(shapefile_path, conn=None):
    conn = conn or db.get_conn()

    print("Loading meter locations...")
    meters_df = load_meters(shapefile_path)
    print(f"  {len(meters_df)} meters with a valid surveyed location")

    print("Matching to parcels (point-in-polygon, nearest-parcel fallback within "
          f"{NEAREST_FALLBACK_MAX_M}m)...")
    meters_df = match_parcels(meters_df, conn)
    matched = meters_df["parcel_id"].notna().sum()
    total = len(meters_df)
    method_counts = meters_df["match_method"].value_counts(dropna=True)
    print(f"  {matched} / {total} meters matched to a parcel ({matched / total:.1%}): "
          + ", ".join(f"{v} {k}" for k, v in method_counts.items()))

    rows = meters_df.where(pd.notna(meters_df), None).to_dict("records")
    db.replace_gis_meters(conn, rows)
    return {"total": total, "matched": int(matched)}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 import_meter_locations.py <path to WATER_Meter.zip or .shp>")
        sys.exit(1)
    run(sys.argv[1])
