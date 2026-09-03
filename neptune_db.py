"""SQLite storage layer for Neptune 360 data.

Two main tables:
  - customers: one row per meter/endpoint (account_number + premise_key + meter info).
               NOTE: Neptune's API exposes no customer name/phone/email — only
               account/premise/meter identifiers. See README for details.
  - water_usage: one row per meter per reading date (consumption history).

  - customer_billing: name/address/phone/email, imported from the billing
    system's spreadsheet export (not from Neptune). Joins to customers/water_usage
    on meter_id == miu_id (unique) or account_number.

  - app_users: login accounts for the Streamlit app itself (email + hashed
    password + role), used when NEPTUNE_PUBLIC_MODE is on. Unrelated to
    Neptune/billing data — this is who's allowed to look at it.

Plus two bookkeeping tables:
  - api_call_log: every API call made, timestamped, for daily budget tracking.
  - sync_state: generic key/value store for resumable backfill cursors.
"""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "neptune.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    miu_id              TEXT PRIMARY KEY,
    site_id             TEXT,
    account_number      TEXT,
    premise_key         TEXT,
    register_id         TEXT,
    meter_number        TEXT,
    meter_type          TEXT,
    meter_size          TEXT,
    meter_manufacturer  TEXT,
    dials               TEXT,
    multiplier          TEXT,
    unit_of_measure     TEXT,
    cycle_route         TEXT,
    synced_at           TEXT
);

CREATE TABLE IF NOT EXISTS water_usage (
    site_id                      TEXT,
    miu_id                       TEXT,
    meter_number                 TEXT,
    reading_date                 TEXT,
    consumption                  REAL,
    consumption_with_multiplier  REAL,
    synced_at                    TEXT,
    PRIMARY KEY (site_id, miu_id, reading_date)
);

CREATE TABLE IF NOT EXISTS api_call_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    endpoint TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Cheap recent-usage ranking used to pick which meters get a full deep-dive
-- backfill when the daily/session call budget can't cover everyone.
CREATE TABLE IF NOT EXISTS usage_scan (
    miu_id            TEXT PRIMARY KEY,
    meter_number      TEXT,
    window_begin      TEXT,
    window_end        TEXT,
    total_consumption REAL,
    points            INTEGER,
    scanned_at        TEXT
);

-- Customer name/address/contact info from the billing system's spreadsheet
-- export. NOT from Neptune. meter_id lines up with customers.miu_id.
CREATE TABLE IF NOT EXISTS customer_billing (
    meter_id         TEXT PRIMARY KEY,
    account_number   TEXT,
    customer_name    TEXT,
    location         TEXT,
    location_no      TEXT,
    parcel_id        TEXT,
    primary_phone    TEXT,
    secondary_phone  TEXT,
    email_address    TEXT,
    source_file      TEXT,
    imported_at      TEXT
);

-- Login accounts for the app itself. role is a three-tier ladder:
--   viewer  — Customers / Water Usage / Continuous Users
--   admin   — viewer, plus can create viewer/admin accounts (not global)
--   global  — everything, including Sync & Backfill and Ask AI
CREATE TABLE IF NOT EXISTS app_users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('viewer', 'admin', 'global')),
    active        INTEGER NOT NULL DEFAULT 1,
    created_by    TEXT,
    created_at    TEXT NOT NULL
);

-- County parcel boundaries, imported from a GIS export (see import_gis.py).
-- NOT from Neptune or the billing system. geometry is a GeoJSON geometry
-- object stored as text (one polygon/multipolygon per parcel), in WGS84
-- (EPSG:4326) — not suitable for area math directly (degrees, not a linear
-- unit), which is why area_sqft/area_acres are precomputed at import time
-- by reprojecting into a local metric CRS first (see import_gis.py).
CREATE TABLE IF NOT EXISTS parcels (
    parcel_id   TEXT PRIMARY KEY,
    address     TEXT,
    city        TEXT,
    geometry    TEXT NOT NULL,
    area_sqft   REAL,
    area_acres  REAL,
    imported_at TEXT
);

-- Which parcel each meter sits on — keyed by meter_id (== customers.miu_id),
-- matching customer_billing's own primary key, since one account_number can
-- legitimately cover multiple meters/locations. match_method records how we
-- know: 'geocoded' (address -> coordinates -> point-in-polygon, the
-- confident path) vs 'address_exact'/'address_prefix' (string-matched as a
-- fallback for whatever the geocoder couldn't place — see import_gis.py).
CREATE TABLE IF NOT EXISTS meter_parcels (
    meter_id       TEXT PRIMARY KEY,
    parcel_id      TEXT NOT NULL,
    match_method   TEXT NOT NULL,
    lat            REAL,
    lon            REAL,
    matched_at     TEXT
);

-- The utility's own meter-inventory GIS layer (see import_meter_locations.py)
-- — a shapefile export with a surveyed lat/lon per physical meter. meter_id
-- here is the utility's internal asset ID ('MeterID' in the shapefile), NOT
-- Neptune's miu_id/meter_number — the two ID spaces don't overlap (checked:
-- 9 coincidental matches out of ~2,650), so this table stands alone rather
-- than joining to customers/customer_billing. parcel_id comes from a direct
-- point-in-polygon match against the already-imported `parcels` table using
-- the surveyed coordinates — no geocoding needed since these are real GPS
-- points, not addresses.
CREATE TABLE IF NOT EXISTS gis_meters (
    meter_id      TEXT PRIMARY KEY,
    lat           REAL NOT NULL,
    lon           REAL NOT NULL,
    address       TEXT,
    customer_name TEXT,
    meter_size    TEXT,
    service_type  TEXT,
    install_year  INTEGER,
    parcel_id     TEXT,
    match_method  TEXT,
    match_dist_m  REAL,
    imported_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_water_usage_miu ON water_usage(miu_id);
CREATE INDEX IF NOT EXISTS idx_water_usage_date ON water_usage(reading_date);
CREATE INDEX IF NOT EXISTS idx_customers_account ON customers(account_number);
CREATE INDEX IF NOT EXISTS idx_billing_account ON customer_billing(account_number);
CREATE INDEX IF NOT EXISTS idx_app_users_email ON app_users(email);
CREATE INDEX IF NOT EXISTS idx_meter_parcels_parcel ON meter_parcels(parcel_id);
CREATE INDEX IF NOT EXISTS idx_gis_meters_parcel ON gis_meters(parcel_id);
"""

PBKDF2_ITERATIONS = 200_000
VALID_ROLES = ("viewer", "admin", "global")

# Columns added after a table's original CREATE TABLE IF NOT EXISTS — that
# statement is a no-op on a database that already has the table, so new
# columns need an explicit ALTER TABLE on existing installs. Keyed by table,
# each value is (column_name, column_type_and_default) as it'd appear in
# ADD COLUMN.
_COLUMN_MIGRATIONS = {
    "parcels": [
        ("area_sqft", "REAL"),
        ("area_acres", "REAL"),
    ],
}


def _migrate_schema(conn):
    for table, columns in _COLUMN_MIGRATIONS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, coltype in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}")
    conn.commit()


def get_conn(readonly=False):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    if readonly:
        # Used by the AI Q&A path — a connection that physically cannot write,
        # regardless of what SQL text reaches it.
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    else:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        # WAL mode lets one writer and many readers hit the file concurrently
        # without stepping on each other. Sticks in the file header once set,
        # so every connection (including readonly ones) benefits from here on.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.commit()
        conn.row_factory = sqlite3.Row
        _migrate_schema(conn)
        ensure_seed_global_user(
            conn,
            os.environ.get("NEPTUNE_SEED_GLOBAL_EMAIL", ""),
            os.environ.get("NEPTUNE_SEED_GLOBAL_PASSWORD", ""),
        )
    # If another connection (e.g. a second browser tab's session) is mid-write,
    # wait up to 5s for it to finish instead of failing immediately.
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat()


def upsert_customers(conn, rows):
    """rows: list of dicts matching EndpointsV2ResponseEndpoint fields."""
    now = _now()
    conn.executemany(
        """
        INSERT INTO customers (miu_id, site_id, account_number, premise_key, register_id,
                                meter_number, meter_type, meter_size, meter_manufacturer,
                                dials, multiplier, unit_of_measure, cycle_route, synced_at)
        VALUES (:miu_id, :site_id, :account_number, :premise_key, :register_id,
                :meter_number, :meter_type, :meter_size, :meter_manufacturer,
                :dials, :multiplier, :unit_of_measure, :cycle_route, :synced_at)
        ON CONFLICT(miu_id) DO UPDATE SET
            site_id=excluded.site_id, account_number=excluded.account_number,
            premise_key=excluded.premise_key, register_id=excluded.register_id,
            meter_number=excluded.meter_number, meter_type=excluded.meter_type,
            meter_size=excluded.meter_size, meter_manufacturer=excluded.meter_manufacturer,
            dials=excluded.dials, multiplier=excluded.multiplier,
            unit_of_measure=excluded.unit_of_measure, cycle_route=excluded.cycle_route,
            synced_at=excluded.synced_at
        """,
        [{**r, "synced_at": now} for r in rows],
    )
    conn.commit()


def upsert_water_usage(conn, rows):
    """rows: list of dicts with site_id, miu_id, meter_number, reading_date,
    consumption, consumption_with_multiplier."""
    now = _now()
    conn.executemany(
        """
        INSERT INTO water_usage (site_id, miu_id, meter_number, reading_date,
                                  consumption, consumption_with_multiplier, synced_at)
        VALUES (:site_id, :miu_id, :meter_number, :reading_date,
                :consumption, :consumption_with_multiplier, :synced_at)
        ON CONFLICT(site_id, miu_id, reading_date) DO UPDATE SET
            meter_number=excluded.meter_number,
            consumption=excluded.consumption,
            consumption_with_multiplier=excluded.consumption_with_multiplier,
            synced_at=excluded.synced_at
        """,
        [{**r, "synced_at": now} for r in rows],
    )
    conn.commit()


def record_api_call(conn, endpoint):
    conn.execute("INSERT INTO api_call_log (ts, endpoint) VALUES (?, ?)", (_now(), endpoint))
    conn.commit()


def calls_today(conn, utc_date=None):
    utc_date = utc_date or datetime.now(timezone.utc).date().isoformat()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM api_call_log WHERE substr(ts, 1, 10) = ?", (utc_date,)
    ).fetchone()
    return row["n"]


def get_sync_state(conn, key):
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None


def set_sync_state(conn, key, value):
    conn.execute(
        "INSERT INTO sync_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )
    conn.commit()


def clear_sync_state(conn, key):
    conn.execute("DELETE FROM sync_state WHERE key = ?", (key,))
    conn.commit()


def upsert_customer_billing(conn, rows, source_file):
    """rows: list of dicts with meter_id, account_number, customer_name, location,
    location_no, parcel_id, primary_phone, secondary_phone, email_address."""
    now = _now()
    conn.executemany(
        """
        INSERT INTO customer_billing (meter_id, account_number, customer_name, location,
                                       location_no, parcel_id, primary_phone, secondary_phone,
                                       email_address, source_file, imported_at)
        VALUES (:meter_id, :account_number, :customer_name, :location, :location_no,
                :parcel_id, :primary_phone, :secondary_phone, :email_address,
                :source_file, :imported_at)
        ON CONFLICT(meter_id) DO UPDATE SET
            account_number=excluded.account_number, customer_name=excluded.customer_name,
            location=excluded.location, location_no=excluded.location_no,
            parcel_id=excluded.parcel_id, primary_phone=excluded.primary_phone,
            secondary_phone=excluded.secondary_phone, email_address=excluded.email_address,
            source_file=excluded.source_file, imported_at=excluded.imported_at
        """,
        [{**r, "source_file": source_file, "imported_at": now} for r in rows],
    )
    conn.commit()


def upsert_usage_scan(conn, rows):
    """rows: list of dicts with miu_id, meter_number, window_begin, window_end,
    total_consumption, points."""
    now = _now()
    conn.executemany(
        """
        INSERT INTO usage_scan (miu_id, meter_number, window_begin, window_end,
                                 total_consumption, points, scanned_at)
        VALUES (:miu_id, :meter_number, :window_begin, :window_end,
                :total_consumption, :points, :scanned_at)
        ON CONFLICT(miu_id) DO UPDATE SET
            meter_number=excluded.meter_number, window_begin=excluded.window_begin,
            window_end=excluded.window_end, total_consumption=excluded.total_consumption,
            points=excluded.points, scanned_at=excluded.scanned_at
        """,
        [{**r, "scanned_at": now} for r in rows],
    )
    conn.commit()


def counts(conn):
    c = conn.execute("SELECT COUNT(*) AS n FROM customers").fetchone()["n"]
    w = conn.execute("SELECT COUNT(*) AS n FROM water_usage").fetchone()["n"]
    return {"customers": c, "water_usage_rows": w}


# ---- app users (login accounts, not Neptune/billing data) ---------------

def _hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{salt.hex()}${digest.hex()}"


def _verify_password(password, stored):
    try:
        salt_hex, _ = stored.split("$", 1)
    except ValueError:
        return False  # malformed hash — never happens unless the DB is hand-edited
    salt = bytes.fromhex(salt_hex)
    return hmac.compare_digest(_hash_password(password, salt), stored)


def create_user(conn, email, password, role, created_by=None):
    if role not in VALID_ROLES:
        raise ValueError(f"invalid role: {role!r}")
    email = email.strip().lower()
    conn.execute(
        "INSERT INTO app_users (email, password_hash, role, active, created_by, created_at) "
        "VALUES (?, ?, ?, 1, ?, ?)",
        (email, _hash_password(password), role, created_by, _now()),
    )
    conn.commit()


def get_user_by_email(conn, email):
    row = conn.execute(
        "SELECT * FROM app_users WHERE email = ?", (email.strip().lower(),)
    ).fetchone()
    return dict(row) if row else None


def verify_user_password(conn, email, password):
    """Returns the user dict on success, else None (unknown email, wrong
    password, and a deactivated account all fail the same way — no signal to
    an attacker about which)."""
    user = get_user_by_email(conn, email)
    if not user or not user["active"]:
        return None
    if not _verify_password(password, user["password_hash"]):
        return None
    return user


def list_users(conn):
    rows = conn.execute(
        "SELECT id, email, role, active, created_by, created_at FROM app_users "
        "ORDER BY created_at"
    ).fetchall()
    return [dict(r) for r in rows]


def set_user_active(conn, user_id, active):
    conn.execute("UPDATE app_users SET active = ? WHERE id = ?", (1 if active else 0, user_id))
    conn.commit()


def count_users_with_role(conn, role, active_only=False):
    q = "SELECT COUNT(*) AS n FROM app_users WHERE role = ?"
    params = [role]
    if active_only:
        q += " AND active = 1"
    return conn.execute(q, params).fetchone()["n"]


def ensure_seed_global_user(conn, email, password):
    """Bootstraps the first global account from NEPTUNE_SEED_GLOBAL_EMAIL/
    _PASSWORD in .env, but only if NO global account exists yet (active or
    not) — so it never fights a password change made later through the app,
    and never re-creates one someone deliberately deactivated."""
    if not email or not password:
        return
    if count_users_with_role(conn, "global") > 0:
        return
    create_user(conn, email, password, "global", created_by="seed")


# ---- GIS: parcels + account-to-parcel matches (see import_gis.py) --------

def replace_parcels(conn, rows):
    """rows: list of dicts with parcel_id, address, city, geometry (a GeoJSON
    geometry object already JSON-encoded as a string), area_sqft, area_acres.
    Full replace each run — parcel data is re-imported wholesale, not
    upserted row by row."""
    now = _now()
    conn.execute("DELETE FROM parcels")
    conn.executemany(
        "INSERT INTO parcels (parcel_id, address, city, geometry, area_sqft, area_acres, imported_at) "
        "VALUES (:parcel_id, :address, :city, :geometry, :area_sqft, :area_acres, :imported_at)",
        [{**r, "imported_at": now} for r in rows],
    )
    conn.commit()


def replace_meter_parcels(conn, rows):
    """rows: list of dicts with meter_id, parcel_id, match_method, lat, lon,
    matched_at. Full replace each run, same reasoning as replace_parcels."""
    conn.execute("DELETE FROM meter_parcels")
    conn.executemany(
        "INSERT INTO meter_parcels (meter_id, parcel_id, match_method, lat, lon, matched_at) "
        "VALUES (:meter_id, :parcel_id, :match_method, :lat, :lon, :matched_at)",
        rows,
    )
    conn.commit()


def upsert_meter_parcels(conn, rows):
    """rows: list of dicts with meter_id, parcel_id, match_method, lat, lon,
    matched_at. Unlike replace_meter_parcels (full wipe, used by
    import_gis.py's billing-address pass), this upserts just the given
    meter_ids — used by import_meter_locations.py to layer higher-confidence
    GPS-survey matches ('gis_survey') on top of the existing geocoded/
    address-string matches, overriding a given meter_id's row only when the
    survey actually covers it."""
    now = _now()
    conn.executemany(
        "INSERT INTO meter_parcels (meter_id, parcel_id, match_method, lat, lon, matched_at) "
        "VALUES (:meter_id, :parcel_id, :match_method, :lat, :lon, :matched_at) "
        "ON CONFLICT(meter_id) DO UPDATE SET "
        "parcel_id = excluded.parcel_id, match_method = excluded.match_method, "
        "lat = excluded.lat, lon = excluded.lon, matched_at = excluded.matched_at",
        [{**r, "matched_at": r.get("matched_at") or now} for r in rows],
    )
    conn.commit()


def replace_gis_meters(conn, rows):
    """rows: list of dicts with meter_id, lat, lon, address, customer_name,
    meter_size, service_type, install_year, parcel_id, match_method,
    match_dist_m. Full replace each run (see import_meter_locations.py) —
    same reasoning as replace_parcels."""
    now = _now()
    conn.execute("DELETE FROM gis_meters")
    conn.executemany(
        "INSERT INTO gis_meters (meter_id, lat, lon, address, customer_name, meter_size, "
        "service_type, install_year, parcel_id, match_method, match_dist_m, imported_at) "
        "VALUES (:meter_id, :lat, :lon, :address, :customer_name, :meter_size, "
        ":service_type, :install_year, :parcel_id, :match_method, :match_dist_m, :imported_at)",
        [{**r, "imported_at": now} for r in rows],
    )
    conn.commit()
