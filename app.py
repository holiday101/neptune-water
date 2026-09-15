"""Streamlit interface: sync from Neptune 360, view Customers / Water Usage,
ask AI questions over the local SQLite data.

Run with:  streamlit run app.py
"""
import json
import math
import os
import re
import time
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pydeck as pdk
import streamlit as st
from dotenv import load_dotenv

import neptune_db as db
import neptune_sync as sync
from neptune_client import BudgetExceeded, NeptuneAPIError, NeptuneClient

load_dotenv()

st.set_page_config(page_title="GotLeaks AI", page_icon="💧", layout="wide")


def _fmt_dt(value, with_time=True):
    """Formats an ISO datetime/date string (or pandas Timestamp) for display,
    e.g. 'Aug 25, 2026 2:02 AM' instead of '2026-08-25T02:02:40.457007+00:00'.
    Returns '' for missing values so it's safe to map over a column with gaps."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        return ""
    return ts.strftime("%b %-d, %Y %-I:%M %p") if with_time else ts.strftime("%b %-d, %Y")


def _leak_color(min_consumption, threshold):
    """Fill color for a parcel on the map: neutral blue if not currently a
    continuous leak, yellow-to-red by severity if it is. Log-scaled since
    continuous rates span a huge range in practice (single digits up to a
    couple thousand gal/hr for a stuck commercial valve) — a linear scale
    would make everything above a modest leak look equally maxed-out red."""
    if min_consumption is None or pd.isna(min_consumption):
        return [70, 130, 220, 60]
    t = min(math.log(max(min_consumption, threshold) / threshold + 1) / math.log(50), 1.0)
    return [255, int(215 * (1 - t)), 0, 200]


def _haversine_ft(lat1, lon1, lat2, lon2):
    """Great-circle distance in feet between one (lat1, lon1) point and one or
    more (lat2, lon2) points (numpy arrays or scalars) -- used to rank a
    meter's nearest neighbors by surveyed GPS location."""
    r_m = 6371000.0
    phi1, phi2 = np.radians(lat1), np.radians(lat2)
    dphi = np.radians(np.asarray(lat2, dtype=float) - lat1)
    dlambda = np.radians(np.asarray(lon2, dtype=float) - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(dlambda / 2) ** 2
    dist_m = 2 * r_m * np.arcsin(np.sqrt(a))
    return dist_m * 3.28084


# ---- cached queries -------------------------------------------------------
# water_usage has grown into the tens of millions of rows. Streamlit reruns
# the WHOLE script — every `with tab_x:` block, not just the visible tab — on
# every single interaction anywhere in the app (typing in a filter box,
# logging in, clicking any button). `_conn` (underscore prefix) tells
# st.cache_data to skip trying to hash the connection object itself.
#
# The genuinely expensive aggregations over the full water_usage table
# (leak/continuous-usage status, the row-count shown in the sidebar) are no
# longer computed here at all -- they're precomputed by
# neptune_db.recompute_leak_status(), run as part of each sync (see
# neptune_sync.py), and these functions just read the small results back.
# That's what makes it safe for every sync path to still call
# st.cache_data.clear() on success (see _render_sync_tab): every remaining
# cached function below is bounded by meter/parcel count, not by how big
# water_usage has grown, so a cold cache is cheap regardless.
#
# No ttl: these only need to be right immediately after a sync, and that's
# exactly when the cache gets cleared. A time-based expiry would only add
# back a slow-ish path on a schedule for no benefit (and used to actively
# hurt: a restart during deploys already empties this in-memory cache, so a
# TTL on top of that meant anyone who'd been away for a while paid the cost
# all over again). Restarting the service (or running import_billing.py /
# any direct DB edit outside the app) also clears it, since it's process
# memory.
@st.cache_data
def _cached_counts(_conn):
    return db.counts(_conn)


@st.cache_data
def _cached_customers_df(_conn):
    # Lot size comes from the parcel matched to this meter (see
    # meter_parcels / import_gis.py) -- not every meter has a matched parcel,
    # so area_sqft/lot_zone are NULL for those (LEFT JOINs throughout).
    # Zones are computed by range joins against lot_size_zones /
    # building_size_zones (see SCHEMA / ensure_seed_*_zones in neptune_db.py)
    # rather than stored, so editing a zone boundary there takes effect
    # immediately. building_sqft has no import source yet (nothing populates
    # parcels.building_sqft currently -- see that column's comment in
    # neptune_db.py and the backlog doc), so bldg_zone is NULL for everyone
    # until that data gets imported; the join is here and ready for it.
    return pd.read_sql_query(
        """
        SELECT c.account_number, b.customer_name, b.location, b.primary_phone,
               b.secondary_phone, b.email_address, c.meter_number, c.miu_id,
               c.cycle_route, c.meter_type, c.meter_size, b.parcel_id,
               ROUND(p.area_sqft) AS lot_size_sqft, lz.zone AS lot_zone, lz.label AS lot_zone_label,
               ROUND(p.building_sqft) AS building_sqft, bz.zone AS bldg_zone, bz.label AS bldg_zone_label
        FROM customers c
        LEFT JOIN customer_billing b ON b.meter_id = c.miu_id
        LEFT JOIN meter_parcels mp ON mp.meter_id = c.miu_id
        LEFT JOIN parcels p ON p.parcel_id = mp.parcel_id
        LEFT JOIN lot_size_zones lz
            ON p.area_sqft >= lz.min_sqft
           AND (lz.max_sqft IS NULL OR p.area_sqft < lz.max_sqft)
        LEFT JOIN building_size_zones bz
            ON p.building_sqft >= bz.min_sqft
           AND (bz.max_sqft IS NULL OR p.building_sqft < bz.max_sqft)
        ORDER BY c.account_number
        """,
        _conn,
    )


@st.cache_data
def _cached_usage_meters(_conn):
    # EXISTS instead of `miu_id IN (SELECT DISTINCT miu_id FROM water_usage)`:
    # the IN-subquery form did a full DISTINCT scan of water_usage (tens of
    # millions of rows); EXISTS lets SQLite do one indexed lookup per
    # customer row instead (~2,700 of them) -- measured ~30x faster on the
    # real data, same result.
    return pd.read_sql_query(
        "SELECT DISTINCT c.miu_id, c.account_number, c.meter_number, b.customer_name "
        "FROM customers c LEFT JOIN customer_billing b ON b.meter_id = c.miu_id "
        "WHERE EXISTS (SELECT 1 FROM water_usage w WHERE w.miu_id = c.miu_id) "
        "ORDER BY b.customer_name, c.account_number",
        _conn,
    )


@st.cache_data
def _cached_leak_status(_conn):
    """Trailing-7-day leak/continuous-usage status per meter, precomputed by
    neptune_db.recompute_leak_status() as part of each sync (see
    neptune_sync.py) rather than live here. water_usage grew into the tens
    of millions of rows and this used to be two full scans of it
    (_cached_continuous_users + _cached_streak_starts, see git history) —
    expensive enough to make every cache-cold page load (i.e. right after
    any sync, since sync clears the whole Streamlit cache) take 30-60+
    seconds. Moving the aggregation into the sync job itself means the app
    only ever does a cheap read of a ~meter-count-sized table.

    Returns the same qualifying ("zero-free week", >7 readings), meta-joined
    shape the old two functions used to produce together -- including
    window_start/window_end (as of the last sync) and streak_start /
    since_data_began, so callers don't need a separate merge.
    """
    cols = [
        "miu_id", "window_start", "window_end", "meter_number", "account_number",
        "customer_name", "address", "primary_phone", "secondary_phone", "email_address",
        "reading_count", "zero_count", "min_consumption", "roll3_min_consumption",
        "total_consumption", "streak_start", "since_data_began",
    ]
    status = pd.read_sql_query("SELECT * FROM meter_leak_status", _conn)
    if status.empty:
        return pd.DataFrame(columns=cols)

    qualifying = status[(status["zero_count"] == 0) & (status["reading_count"] > 7)]
    if qualifying.empty:
        return qualifying.reindex(columns=cols)

    # location/phones/email come from customer_billing (see README -- Neptune
    # itself exposes no contact info) so these are NULL for any meter with no
    # billing-import match, same as customer_name already was.
    meta = pd.read_sql_query(
        "SELECT c.miu_id, c.meter_number, c.account_number, b.customer_name, "
        "       b.location AS address, b.primary_phone, b.secondary_phone, b.email_address "
        "FROM customers c LEFT JOIN customer_billing b ON b.meter_id = c.miu_id",
        _conn,
    )
    return (
        qualifying.merge(meta, on="miu_id", how="left")
        .sort_values("min_consumption", ascending=False)
        .reset_index(drop=True)
    )


@st.cache_data
def _cached_meter_coords(_conn):
    """One row per meter with usage data AND a known lat/lon, for the
    nearest-neighbor usage comparison in _render_usage_chart. lat/lon comes
    from meter_parcels (populated for 'geocoded' and 'gis_survey' matches;
    'address_exact'/'address_prefix' matches have no coordinate and are
    excluded here -- string-matched only, nothing to measure distance from)."""
    return pd.read_sql_query(
        """
        SELECT c.miu_id, mp.lat, mp.lon, b.customer_name, b.location AS address,
               lz.zone AS lot_zone, lz.label AS lot_zone_label
        FROM customers c
        JOIN meter_parcels mp ON mp.meter_id = c.miu_id AND mp.lat IS NOT NULL
        LEFT JOIN customer_billing b ON b.meter_id = c.miu_id
        LEFT JOIN parcels p ON p.parcel_id = mp.parcel_id
        LEFT JOIN lot_size_zones lz
            ON p.area_sqft >= lz.min_sqft
           AND (lz.max_sqft IS NULL OR p.area_sqft < lz.max_sqft)
        WHERE EXISTS (SELECT 1 FROM water_usage w WHERE w.miu_id = c.miu_id)
        """,
        _conn,
    )


@st.cache_data
def _cached_meter_parcels(_conn):
    """Every meter with a resolved parcel (see import_gis.py), joined to its
    parcel geometry and customer info. match_method distinguishes a
    geocoded (confident) match from a string-matched fallback one."""
    return pd.read_sql_query(
        """
        SELECT mp.meter_id, mp.parcel_id, mp.match_method,
               p.geometry, p.address AS parcel_address,
               c.account_number, c.meter_number, b.customer_name
        FROM meter_parcels mp
        JOIN parcels p ON p.parcel_id = mp.parcel_id
        JOIN customers c ON c.miu_id = mp.meter_id
        LEFT JOIN customer_billing b ON b.meter_id = mp.meter_id
        """,
        _conn,
    )


@st.cache_data
def _cached_gis_meters(_conn):
    """Surveyed meter locations from the utility's own GIS export (see
    import_meter_locations.py) — a different source and ID space from
    meter_parcels above (that one matches Neptune meters via billing-address
    geocoding; this one is the utility's own asset inventory, matched to a
    parcel directly from its surveyed GPS coordinate). Not joined to
    customers/water_usage since meter_id here doesn't line up with miu_id."""
    return pd.read_sql_query(
        """
        SELECT g.meter_id, g.lat, g.lon, g.address, g.customer_name,
               g.meter_size, g.parcel_id, g.match_method, g.match_dist_m,
               p.geometry AS parcel_geometry
        FROM gis_meters g
        LEFT JOIN parcels p ON p.parcel_id = g.parcel_id
        """,
        _conn,
    )

# PUBLIC_MODE gates the whole app behind per-user email/password login, for
# deployments meant to be reachable from the open internet. Three-tier role
# ladder (see app_users table in neptune_db.py):
#   viewer  — Customers / Water Usage / Continuous Users
#   admin   — viewer, plus Manage Users (can create viewer/admin accounts)
#   global  — everything, including Sync & Backfill and Ask AI
# All tabs are always visible; ones above your role show a locked message
# instead of their real content. See README "Public deployment" section.
PUBLIC_MODE = os.getenv("NEPTUNE_PUBLIC_MODE", "false").strip().lower() == "true"

# Sync & Backfill calls Neptune's real API, which enforces its OWN daily
# quota server-side — but this app's budget tracking (api_call_log,
# NEPTUNE_DAILY_CALL_BUDGET) only counts calls made from *this* SQLite db.
# Running a sync from a laptop against the same Neptune site the production
# server is backfilling from spends real quota that the server's own count
# has no way to see, silently throwing off its budget math and resumable
# backfill. So sync is opt-in per machine, off by default (i.e. off on any
# local checkout that hasn't explicitly turned it on) — set
# NEPTUNE_ALLOW_SYNC=true only in the one place that should actually be
# hitting the API (see README "Public deployment").
ALLOW_SYNC = os.getenv("NEPTUNE_ALLOW_SYNC", "false").strip().lower() == "true"

ROLE_RANK = {"viewer": 0, "admin": 1, "global": 2}

# Per-session lockout after too many wrong passwords. This is a *session*
# lockout (each open browser tab/websocket connection has its own
# st.session_state), not a global one — it stops one held-open tab from being
# used to brute-force guesses, complementing nginx's connection-rate limit on
# the /water/ location, which throttles how fast an attacker can open *new*
# sessions to sidestep this. Neither alone is complete; together they cover
# both angles.
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_SECONDS = 60


def _rate_limited(key):
    locked_until = st.session_state.get(f"{key}_locked_until", 0)
    if time.time() < locked_until:
        st.error(f"Too many incorrect attempts. Try again in {int(locked_until - time.time())}s.")
        return True
    return False


def _record_failure(key):
    attempts = st.session_state.get(f"{key}_attempts", 0) + 1
    st.session_state[f"{key}_attempts"] = attempts
    if attempts >= MAX_LOGIN_ATTEMPTS:
        st.session_state[f"{key}_locked_until"] = time.time() + LOCKOUT_SECONDS
        st.session_state[f"{key}_attempts"] = 0


def _record_success(key):
    st.session_state.pop(f"{key}_attempts", None)
    st.session_state.pop(f"{key}_locked_until", None)


def get_conn():
    # Session-scoped, NOT @st.cache_resource: that would cache one connection
    # object for the whole server process and share it across every browser
    # tab/session. Streamlit runs each session on its own thread, and a raw
    # sqlite3.Connection isn't safe for two threads to use at once even with
    # check_same_thread=False (that flag only disables Python's safety check,
    # it doesn't add real thread-safety) — concurrent use crashes with
    # "sqlite3.InterfaceError: bad parameter or other API misuse". Keeping one
    # connection per session avoids that; PRAGMA busy_timeout in neptune_db.py
    # handles the remaining case of two sessions' connections both hitting the
    # file at once.
    if "db_conn" not in st.session_state:
        st.session_state.db_conn = db.get_conn()
    return st.session_state.db_conn


def get_client(conn):
    try:
        return NeptuneClient(conn=conn)
    except KeyError as e:
        st.error(f"Missing required setting in .env: {e}. Copy .env.example to .env and fill it in.")
        st.stop()


conn = get_conn()  # needed before the login gate below — it looks users up in app_users


def _check_password():
    """Email/password login gate. Returns True once a valid account has
    logged in this browser session (setting st.session_state.role/email);
    otherwise renders a login form and returns False so the caller can
    st.stop()."""
    if st.session_state.get("authenticated"):
        return True

    st.title("🔒 GotLeaks AI")
    if _rate_limited("login"):
        return False
    with st.form("login", clear_on_submit=True):
        email = st.text_input("Email")
        pw = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Log in")
    if submitted:
        user = db.verify_user_password(conn, email, pw)
        if user:
            _record_success("login")
            st.session_state.authenticated = True
            st.session_state.role = user["role"]
            st.session_state.email = user["email"]
            st.rerun()
        else:
            _record_failure("login")
            st.error("Incorrect email or password.")
    return False


def _locked_message(tab_label, required_role):
    st.info(f"🔒 {tab_label} requires a **{required_role}** account.")
    st.caption("Log out and sign in with an account that has access.")


# Keeps water usage roughly current between sync_daily.py's nightly cron runs
# by pulling the last few days once per session, right after login — a top-up
# for "I checked this app mid-afternoon and it's still showing this morning's
# numbers", not a replacement for the cron job. Throttled via sync_state
# (not just st.session_state) so a burst of logins in the same few minutes
# only fires one real sync, not one per visitor. Only does anything at all
# where NEPTUNE_ALLOW_SYNC is on — same reasoning as the Sync tab gate below:
# this app's budget tracking is per-database, so this must stay off on any
# machine that isn't the one deployment meant to be hitting Neptune's API.
AUTO_SYNC_MIN_INTERVAL_MIN = 30
AUTO_SYNC_STATE_KEY = "auto_recent_usage_sync"


def _maybe_auto_sync_recent_usage(conn):
    if not ALLOW_SYNC or st.session_state.get("auto_sync_checked"):
        return
    st.session_state.auto_sync_checked = True

    last = db.get_sync_state(conn, AUTO_SYNC_STATE_KEY)
    if last:
        elapsed_min = (
            datetime.now(timezone.utc) - datetime.fromisoformat(last["synced_at"])
        ).total_seconds() / 60
        if elapsed_min < AUTO_SYNC_MIN_INTERVAL_MIN:
            return

    try:
        client = NeptuneClient(conn=conn)
        rows = sync.sync_water_usage_recent(client, days=3)
        db.set_sync_state(
            conn, AUTO_SYNC_STATE_KEY,
            {"synced_at": datetime.now(timezone.utc).isoformat(), "rows": rows},
        )
        st.cache_data.clear()
        if rows:
            st.toast(f"Refreshed water usage — {rows} new readings.", icon="💧")
    except (KeyError, NeptuneAPIError, BudgetExceeded):
        pass  # best-effort — a sync hiccup should never block someone from using the app


if PUBLIC_MODE and not _check_password():
    st.stop()

_maybe_auto_sync_recent_usage(conn)

_role = "global" if not PUBLIC_MODE else st.session_state.get("role", "viewer")
IS_ADMIN = ROLE_RANK.get(_role, -1) >= ROLE_RANK["admin"]
IS_GLOBAL = _role == "global"

# ---------------------------------------------------------------- sidebar --
st.sidebar.title("💧 GotLeaks AI")
if PUBLIC_MODE:
    st.sidebar.caption(f"Logged in as **{st.session_state.get('email')}** ({_role})")
    if st.sidebar.button("Log out"):
        st.session_state.authenticated = False
        st.session_state.role = None
        st.session_state.email = None
        st.rerun()

if IS_GLOBAL:
    # Neptune API budget/backfill internals — sync-operator info, only shown
    # to global accounts (always shown outside PUBLIC_MODE, since everyone
    # there is effectively global on their own machine).
    try:
        client = get_client(conn)
        remaining = client.calls_remaining()
        used = client.daily_budget - remaining
        st.sidebar.metric("API calls used today", f"{used} / {client.daily_budget}")
        st.sidebar.progress(min(1.0, used / client.daily_budget))
        st.sidebar.caption(f"Site ID: {client.site_id}")
    except Exception:
        client = None
        st.sidebar.warning("Neptune credentials not configured (see .env.example).")
else:
    client = None

row_counts = _cached_counts(conn)
st.sidebar.caption(
    f"{row_counts['customers']} meters/accounts · {row_counts['water_usage_rows']:,} usage rows stored"
)

if IS_GLOBAL:
    progress = sync.backfill_progress(conn)
    if progress:
        st.sidebar.info(
            f"Backfill in progress: {progress['percent_complete']}% "
            f"(resumes automatically from {_fmt_dt(progress['cursor'], with_time=False)})"
        )

tab_customers, tab_usage, tab_continuous, tab_map, tab_users, tab_sync, tab_ai = st.tabs(
    ["👤 Customers", "💧 Water Usage", "🚰 Continuous Users", "🗺️ Map",
     "🧑‍💼 Manage Users", "🔄 Sync & Backfill", "🤖 Ask AI"]
)

# ------------------------------------------------------------- Customers --
with tab_customers:
    st.subheader("Customers")
    billing_count = conn.execute("SELECT COUNT(*) AS n FROM customer_billing").fetchone()["n"]
    if billing_count:
        st.caption(
            f"Meter/account data from Neptune, joined with name/address/phone/email "
            f"imported from the billing system ({billing_count} billing records loaded)."
        )
    else:
        st.caption(
            "Neptune's API does not expose customer names, phone numbers, or emails — "
            "only account numbers, premise keys, and meter details. Run "
            "`python3 import_billing.py \"<path to billing export.xlsx>\"` to add "
            "name/address/contact info from your billing system."
        )
    df = _cached_customers_df(conn)
    if df.empty:
        st.info("No customer data yet. Go to **Sync & Backfill** and click 'Sync Customers'.")
    else:
        q = st.text_input("Filter by name, account number, address, meter number, or cycle route")
        if q:
            mask = df.apply(lambda r: q.lower() in str(r.values).lower(), axis=1)
            df = df[mask]
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.caption(f"{len(df)} rows")

# --------------------------------------------------- shared usage viewer --
# Used by both the Water Usage tab and the Continuous Users click-through
# below. Windows are relative to each meter's own most recent reading, not
# wall-clock "now" — sync can lag behind real time, and a wall-clock window
# would show a misleading trailing gap rather than a full period of data.
USAGE_VIEWS = {
    "Last 7": timedelta(days=7),
    "Today": timedelta(days=1),
    "Month": timedelta(days=30),
    "Year": timedelta(days=365),
    "All": None,
}


def _load_meter_usage(conn, miu_id, view):
    # consumption_with_multiplier only — that's the meter's raw register count
    # already scaled by its fixed multiplier constant into real gallons. Raw
    # `consumption` (pre-multiplier) is never selected: it's not a second
    # measurement, just the same reading before that scaling, and looks wrong
    # (often 10x too high) if read as gallons directly.
    span = USAGE_VIEWS.get(view)
    if span is None:
        df = pd.read_sql_query(
            "SELECT reading_date, consumption_with_multiplier AS gallons_used "
            "FROM water_usage WHERE miu_id = ? ORDER BY reading_date",
            conn, params=(miu_id,),
        )
    else:
        row = conn.execute(
            "SELECT MAX(reading_date) AS m FROM water_usage WHERE miu_id = ?", (miu_id,)
        ).fetchone()
        if not row or not row["m"]:
            return pd.DataFrame(columns=["reading_date", "gallons_used"])
        window_end = pd.Timestamp(row["m"])
        window_start = window_end - span
        df = pd.read_sql_query(
            "SELECT reading_date, consumption_with_multiplier AS gallons_used "
            "FROM water_usage WHERE miu_id = ? AND reading_date > ? AND reading_date <= ? "
            "ORDER BY reading_date",
            conn, params=(miu_id, window_start.isoformat(), row["m"]),
        )
    df["gallons_used"] = df["gallons_used"].round().astype("Int64")
    return df


# Trailing-`days`-day reading_count/total_consumption for every meter,
# computed live -- used only for the leaderboard's "Last 30 days" view;
# meter_leak_status only ever holds the fixed 7-day window computed at sync
# time (see LEAK_STATUS_WINDOW_DAYS in neptune_db.py). Bounded by
# idx_water_usage_date regardless of how large water_usage's full history
# grows (measured ~1s city-wide against 2,600 meters / 11M+ rows), and
# cached via st.cache_data same as everything else here -- cleared on every
# sync, same lifetime as the 7-day figures.
@st.cache_data
def _cached_window_totals(_conn, days):
    row = _conn.execute("SELECT MAX(reading_date) AS m FROM water_usage").fetchone()
    max_date = row["m"] if row else None
    if not max_date:
        return pd.DataFrame(columns=["miu_id", "reading_count", "total_consumption"])
    window_start = (pd.Timestamp(max_date) - timedelta(days=days)).isoformat()
    return pd.read_sql_query(
        "SELECT miu_id, COUNT(*) AS reading_count, "
        "SUM(consumption_with_multiplier) AS total_consumption "
        "FROM water_usage WHERE reading_date > ? AND reading_date <= ? GROUP BY miu_id",
        _conn, params=(window_start, max_date),
    )


# Same as _cached_window_totals above, but scoped to a specific set of
# meters (e.g. one meter's nearby neighbors) via an IN-list instead of every
# meter city-wide. Used by the nearby-meter comparison's week/month toggle.
@st.cache_data
def _cached_window_totals_for_meters(_conn, miu_ids, days):
    if not miu_ids:
        return pd.DataFrame(columns=["miu_id", "reading_count", "total_consumption"])
    row = _conn.execute("SELECT MAX(reading_date) AS m FROM water_usage").fetchone()
    max_date = row["m"] if row else None
    if not max_date:
        return pd.DataFrame(columns=["miu_id", "reading_count", "total_consumption"])
    window_start = (pd.Timestamp(max_date) - timedelta(days=days)).isoformat()
    placeholders = ",".join("?" for _ in miu_ids)
    return pd.read_sql_query(
        f"SELECT miu_id, COUNT(*) AS reading_count, "
        f"SUM(consumption_with_multiplier) AS total_consumption "
        f"FROM water_usage WHERE miu_id IN ({placeholders}) "
        f"AND reading_date > ? AND reading_date <= ? GROUP BY miu_id",
        _conn, params=(*miu_ids, window_start, max_date),
    )


# Per-day totals for one meter, trailing `days` days ending at that meter's
# own most recent reading (same relative-window convention as
# _load_meter_usage above) -- lets the daily breakdown under the
# nearby-meter comparison show which specific days drove the total, rather
# than just the window's aggregate total/average.
@st.cache_data
def _cached_daily_usage(_conn, miu_id, days):
    row = _conn.execute(
        "SELECT MAX(reading_date) AS m FROM water_usage WHERE miu_id = ?", (miu_id,)
    ).fetchone()
    if not row or not row["m"]:
        return pd.DataFrame(columns=["day", "gallons"])
    window_end = pd.Timestamp(row["m"])
    window_start = window_end - timedelta(days=days)
    return pd.read_sql_query(
        "SELECT date(reading_date) AS day, "
        "SUM(consumption_with_multiplier) AS gallons "
        "FROM water_usage WHERE miu_id = ? AND reading_date > ? AND reading_date <= ? "
        "GROUP BY date(reading_date) ORDER BY day",
        _conn, params=(miu_id, window_start.isoformat(), row["m"]),
    )


def _render_usage_chart(conn, miu_id, key_prefix, default_view="Last 7"):
    """View-range selector + chart + table + CSV download for one meter."""
    views = list(USAGE_VIEWS.keys())
    view = st.selectbox("View", views, index=views.index(default_view), key=f"{key_prefix}_view")
    usage = _load_meter_usage(conn, miu_id, view)
    if usage.empty:
        st.info("No usage data in this window.")
        return
    usage["reading_date"] = pd.to_datetime(usage["reading_date"])
    usage = usage.rename(columns={"gallons_used": "Gallons Used"})
    st.line_chart(usage.set_index("reading_date")["Gallons Used"])

    display_usage = usage.rename(columns={"reading_date": "Reading Date"}).copy()
    display_usage["Reading Date"] = display_usage["Reading Date"].apply(_fmt_dt)
    st.dataframe(display_usage, use_container_width=True, hide_index=True)

    csv_df = usage.rename(columns={"reading_date": "Reading Date"})
    st.download_button(
        "Download CSV", csv_df.to_csv(index=False),
        file_name=f"{miu_id}_usage.csv", key=f"{key_prefix}_download",
    )

    with st.expander("Compare to nearby meters"):
        window_choice = st.radio(
            "Compare over", ["Last week", "Last month"],
            horizontal=True, key=f"{key_prefix}_compare_window",
        )
        cmp_days = 7 if window_choice == "Last week" else 30
        window_label = window_choice.lower()

        coords = _cached_meter_coords(conn)
        me = coords[coords["miu_id"] == miu_id]
        if me.empty:
            st.caption(
                "No surveyed GPS location for this meter, so a nearby-meter "
                "comparison isn't available."
            )
        else:
            me = me.iloc[0]
            n_choice = st.selectbox(
                "Compare to nearest", [5, 10, 20, 50], index=1,
                key=f"{key_prefix}_neighbor_n",
            )
            others = coords[coords["miu_id"] != miu_id].copy()
            others["distance_ft"] = _haversine_ft(
                me["lat"], me["lon"], others["lat"].to_numpy(), others["lon"].to_numpy()
            )
            nearest = others.nsmallest(n_choice, "distance_ft").copy()

            window_totals = _cached_window_totals_for_meters(
                conn, tuple(nearest["miu_id"]) + (miu_id,), cmp_days
            )
            nearest = nearest.merge(
                window_totals[["miu_id", "total_consumption"]], on="miu_id", how="left"
            )
            nearest["window_avg"] = nearest["total_consumption"] / cmp_days

            my_total = window_totals.loc[window_totals["miu_id"] == miu_id, "total_consumption"]
            my_avg = my_total.iloc[0] / cmp_days if not my_total.empty and pd.notna(my_total.iloc[0]) else None
            neighborhood_avg = nearest["window_avg"].mean()

            if my_avg is not None and pd.notna(neighborhood_avg) and neighborhood_avg > 0:
                pct = (my_avg - neighborhood_avg) / neighborhood_avg * 100
                direction = "higher" if pct >= 0 else "lower"
                st.caption(
                    f"This meter averaged **{my_avg:,.0f} gal/day** over the {window_label} vs "
                    f"**{neighborhood_avg:,.0f} gal/day** average among the "
                    f"{len(nearest)} nearest meters — **{abs(pct):.0f}% {direction}**."
                )
            elif pd.notna(neighborhood_avg):
                st.caption(
                    f"Average among the {len(nearest)} nearest meters: "
                    f"{neighborhood_avg:,.0f} gal/day over the {window_label}. This meter has no "
                    f"usage in the current {window_label} window to compare."
                )
            else:
                st.caption(f"No comparable usage data among nearby meters over the {window_label}.")

            display_neighbors = nearest.copy()
            display_neighbors["customer"] = display_neighbors["customer_name"].where(
                display_neighbors["customer_name"].notna(), "(no billing match)"
            )
            display_neighbors["address"] = display_neighbors["address"].fillna("")
            display_neighbors["lot_zone_label"] = display_neighbors["lot_zone_label"].fillna("")
            display_neighbors["distance_ft"] = display_neighbors["distance_ft"].round().astype("Int64")
            display_neighbors["total_consumption"] = display_neighbors["total_consumption"].round().astype("Int64")
            display_neighbors["window_avg"] = display_neighbors["window_avg"].round(1)
            display_neighbors = display_neighbors[
                [
                    "distance_ft", "customer", "address", "lot_zone_label",
                    "total_consumption", "window_avg",
                ]
            ].rename(
                columns={
                    "distance_ft": "distance (ft)",
                    "lot_zone_label": "lot zone",
                    "total_consumption": f"{window_label} total (gal)",
                    "window_avg": f"{window_label} avg (gal/day)",
                }
            ).sort_values("distance (ft)").reset_index(drop=True)
            st.dataframe(display_neighbors, use_container_width=True, hide_index=True)
            st.caption(
                "Distance is straight-line, from the utility's surveyed GPS meter "
                "locations where available. Usage columns are blank for a neighbor "
                f"with no reading in the current {window_label} window."
            )

        st.markdown("###### Daily usage — this meter")
        daily = _cached_daily_usage(conn, miu_id, cmp_days)
        if daily.empty:
            st.caption("No usage data in this window.")
        else:
            daily_display = daily.copy()
            daily_display["gallons"] = daily_display["gallons"].round().astype("Int64")
            st.bar_chart(daily_display.set_index("day")["gallons"])
            st.dataframe(
                daily_display.rename(columns={"day": "Day", "gallons": "Gallons"}),
                use_container_width=True, hide_index=True,
            )
            st.caption(
                f"Each bar is that day's total gallons, over the {window_label} "
                "shown above -- use it to see which specific days drove the total, "
                "rather than just the average."
            )


# ----------------------------------------------------------- Water Usage --
with tab_usage:
    st.subheader("Water usage history")
    meters = _cached_usage_meters(conn)
    if meters.empty:
        st.info("No water usage data yet. Go to **Sync & Backfill** to pull some.")
    else:
        board_window_choice = st.radio(
            "Leaderboard window", ["Last 7 days", "Last 30 days"],
            horizontal=True, key="usage_leaderboard_window",
        )
        board_days = 7 if board_window_choice == "Last 7 days" else 30
        st.markdown(f"#### Leaderboard — {board_window_choice.lower()}")
        leak_window = pd.read_sql_query(
            "SELECT window_start, window_end FROM meter_leak_status LIMIT 1", conn
        )
        if leak_window.empty:
            st.info(
                "Usage totals haven't been computed for this data yet — they're "
                "refreshed automatically after each sync. Check **Sync & Backfill**, "
                "or run one now."
            )
        else:
            if board_days == 7:
                # Already precomputed by the sync job -- see meter_leak_status /
                # LEAK_STATUS_WINDOW_DAYS in neptune_db.py -- so the default
                # 7-day view stays a cheap read with no live aggregation.
                window_start_str = leak_window["window_start"].iloc[0]
                window_end_str = leak_window["window_end"].iloc[0]
                weekly = pd.read_sql_query(
                    "SELECT miu_id, reading_count, total_consumption FROM meter_leak_status "
                    "WHERE total_consumption IS NOT NULL",
                    conn,
                )
            else:
                # meter_leak_status only ever holds the fixed 7-day window, so
                # "Last 30 days" is computed live instead -- see
                # _cached_window_totals for why that stays cheap even as
                # water_usage's full history keeps growing.
                window_end_str = leak_window["window_end"].iloc[0]
                window_start_str = (
                    pd.Timestamp(window_end_str) - timedelta(days=board_days)
                ).isoformat()
                weekly = _cached_window_totals(conn, board_days)
                weekly = weekly[weekly["total_consumption"].notna()]
            st.caption(
                f"Window: {_fmt_dt(window_start_str)} → {_fmt_dt(window_end_str)}. "
                f"The {board_days}-day avg column is each meter's total gallons in "
                f"that window divided by {board_days} days — an average daily rate, "
                "not just the raw total. Ranked by highest usage first; click any "
                "column header to re-sort."
            )
            customers_df = _cached_customers_df(conn)
            board = weekly.merge(customers_df, on="miu_id", how="left")

            if board.empty:
                st.info(f"No meters had any usage in the {board_window_choice.lower()}.")
            else:
                board["7_day_avg"] = board["total_consumption"] / board_days
                board["lot_zone_label"] = board["lot_zone_label"].fillna("No parcel match")

                zone_options = (
                    board[["lot_zone", "lot_zone_label"]]
                    .dropna(subset=["lot_zone"])
                    .drop_duplicates()
                    .sort_values("lot_zone")["lot_zone_label"]
                    .tolist()
                )
                if "No parcel match" not in zone_options and (board["lot_zone_label"] == "No parcel match").any():
                    zone_options.append("No parcel match")
                zone_choices = st.multiselect(
                    "Filter by lot-size zone",
                    zone_options,
                    default=[],
                    key="usage_leaderboard_zone_filter",
                    help="Leave empty to show every zone.",
                )
                filtered_board = (
                    board[board["lot_zone_label"].isin(zone_choices)] if zone_choices else board
                )

                if filtered_board.empty:
                    st.info("No meters match that zone filter.")
                else:
                    filtered_board = filtered_board.sort_values(
                        "total_consumption", ascending=False
                    ).reset_index(drop=True)

                    display_board = filtered_board.copy()
                    display_board["customer"] = display_board["customer_name"].where(
                        display_board["customer_name"].notna(), "(no billing match)"
                    )
                    display_board["address"] = display_board["location"].fillna("")
                    display_board["total_consumption"] = display_board["total_consumption"].round().astype("Int64")
                    display_board["7_day_avg"] = display_board["7_day_avg"].round(1)
                    display_board.insert(0, "rank", display_board.index + 1)
                    display_board = display_board[
                        [
                            "rank",
                            "customer",
                            "address",
                            "account_number",
                            "meter_number",
                            "lot_zone_label",
                            "total_consumption",
                            "7_day_avg",
                            "reading_count",
                        ]
                    ].rename(
                        columns={
                            "account_number": "account",
                            "meter_number": "meter",
                            "lot_zone_label": "lot zone",
                            "total_consumption": f"{board_days}-day total (gal)",
                            "7_day_avg": f"{board_days}-day avg (gal/day)",
                            "reading_count": f"readings in {board_window_choice.lower()}",
                        }
                    )
                    board_event = st.dataframe(
                        display_board,
                        use_container_width=True,
                        hide_index=True,
                        on_select="rerun",
                        selection_mode="single-row",
                        key="usage_leaderboard_table",
                    )
                    st.caption(f"{len(display_board)} meters with usage in the {board_window_choice.lower()}")
                    st.download_button(
                        "Download CSV",
                        display_board.to_csv(index=False),
                        file_name="water_usage_leaderboard.csv",
                        key="usage_leaderboard_download",
                    )

                    selected_rows = board_event["selection"]["rows"] if board_event else []
                    if selected_rows:
                        sel = filtered_board.iloc[selected_rows[0]]
                        label = (
                            sel["customer_name"] if pd.notna(sel["customer_name"])
                            else f"acct {sel['account_number'] or '(no account)'}"
                        )
                        st.markdown(f"##### Usage for {label} — meter {sel['meter_number']}")
                        _render_usage_chart(
                            conn, sel["miu_id"], key_prefix="usage_leaderboard_selected", default_view="Last 7"
                        )

        st.markdown("---")
        st.markdown("#### Or look up a specific meter")
        options = {
            (
                f"{r.customer_name} — meter {r.meter_number} — acct {r.account_number or '(no account)'}"
                if pd.notna(r.customer_name)
                else f"{r.account_number or '(no account)'} — meter {r.meter_number} — {r.miu_id}"
            ): r.miu_id
            for r in meters.itertuples()
        }
        choice = st.selectbox("Select a meter", list(options.keys()), key="usage_meter_select")
        miu_id = options[choice]
        _render_usage_chart(conn, miu_id, key_prefix="usage")

# -------------------------------------------------------- Continuous Users --
with tab_continuous:
    st.subheader("Continuous water users — this week")
    st.caption(
        "Meters with more than 7 readings in the trailing 7 days, where every one "
        "of those readings had nonzero usage (no zero reads — gaps in the data "
        "are fine, they're just not counted either way; the >7 threshold filters "
        "out meters with only a stray reading or two, which would otherwise look "
        "trivially \"continuous\"). This usually means water is running nonstop — "
        "a running toilet, a stuck irrigation valve, or a leak. Choose below whether "
        "to sort by the raw lowest hourly reading, or by the 3-hour rolling-average "
        "floor, which smooths out single-hour meter-reporting glitches (a low read "
        "that gets made up for in the next hour) to show the real sustained rate."
    )

    has_usage = conn.execute("SELECT 1 FROM water_usage LIMIT 1").fetchone() is not None
    if not has_usage:
        st.info("No water usage data yet. Go to **Sync & Backfill** to pull some.")
    else:
        leak_window = pd.read_sql_query(
            "SELECT window_start, window_end FROM meter_leak_status LIMIT 1", conn
        )
        if leak_window.empty:
            st.info(
                "Leak status hasn't been computed for this data yet — it's refreshed "
                "automatically after each sync. Check **Sync & Backfill**, or run one now."
            )
            continuous = pd.DataFrame()
        else:
            window_start_str = leak_window["window_start"].iloc[0]
            max_date = leak_window["window_end"].iloc[0]

            continuous = _cached_leak_status(conn)
            continuous["continuous_since"] = continuous.apply(
                lambda r: (
                    ("≥ " if r["since_data_began"] == 1 else "") + _fmt_dt(r["streak_start"], with_time=False)
                ) if pd.notna(r["streak_start"]) else "",
                axis=1,
            )

            st.caption(f"Window: {_fmt_dt(window_start_str)} → {_fmt_dt(max_date)}.")

        if continuous.empty:
            st.info(
                "No meters had a zero-free week. Try checking back once more "
                "usage data has synced."
            )
        else:
            sort_choice = st.radio(
                "Sort by",
                ["Lowest hourly reading (raw)", "3-hour rolling floor (smoothed)"],
                horizontal=True,
                key="continuous_sort_by",
                help=(
                    "Raw is the single lowest hourly reading in the week — a guaranteed "
                    "floor, but a one-hour meter-reporting glitch can make it look lower "
                    "than the leak really is. The 3-hour rolling floor averages each "
                    "reading with its two neighbors first, which smooths that out and is "
                    "usually the more accurate 'real' leak size."
                ),
            )
            sort_col = "roll3_min_consumption" if sort_choice.startswith("3-hour") else "min_consumption"

            show_smaller = st.toggle(
                "Also show smaller leaks (5+ gal/hr, instead of just 10+ gal/hr)",
                value=False,
                key="continuous_show_smaller",
            )
            threshold = 5 if show_smaller else 10
            filtered = (
                continuous[continuous[sort_col] >= threshold]
                .sort_values(sort_col, ascending=False)
                .reset_index(drop=True)
            )
            st.caption(
                f"Showing meters with a continuous flow of at least **{threshold} gal/hr** by the "
                f"{'raw' if sort_col == 'min_consumption' else '3-hour rolling'} measure — "
                "below that, a nonstop trickle is more likely a slow drip than a leak worth "
                "a call. Toggle above to lower the bar to 5 gal/hr."
            )

            if filtered.empty:
                st.info(f"No meters had a continuous flow of at least {threshold} gal/hr this week.")
            else:
                display = filtered.copy()
                display["customer"] = display["customer_name"].where(
                    display["customer_name"].notna(), "(no billing match)"
                )
                display["address"] = display["address"].fillna("")
                # One "phone" column, preferring primary and falling back to
                # secondary rather than showing two mostly-empty columns.
                display["phone"] = display["primary_phone"].where(
                    display["primary_phone"].notna(), display["secondary_phone"]
                ).fillna("")
                display["email"] = display["email_address"].fillna("")
                display["roll3_min_consumption"] = display["roll3_min_consumption"].round(1)
                display = display[
                    [
                        "customer",
                        "address",
                        "phone",
                        "email",
                        "account_number",
                        "meter_number",
                        "continuous_since",
                        "min_consumption",
                        "roll3_min_consumption",
                        "total_consumption",
                        "reading_count",
                    ]
                ].rename(
                    columns={
                        "account_number": "account",
                        "meter_number": "meter",
                        "continuous_since": "continuous since",
                        "min_consumption": "lowest hourly reading",
                        "roll3_min_consumption": "3-hr rolling floor",
                        "total_consumption": "total gallons this week",
                        "reading_count": "hourly readings",
                    }
                )
                st.caption(
                    "\"continuous since\" is the last time this meter read zero, plus one "
                    "reading — everything after that has been nonstop. A **≥** date means "
                    "it's never once read zero in all our recorded history, so the real "
                    "start may be earlier than we can see. \"Lowest hourly reading\" is the "
                    "single smallest raw reading in the window. \"3-hr rolling floor\" "
                    "averages each reading with its two neighbors first, smoothing out "
                    "single-hour reporting glitches (a low read that gets made up for in "
                    "the next hour) — usually the more accurate real leak size. This list "
                    "is sorted and filtered by whichever one is selected above."
                )
                st.caption("Click a row to see that meter's usage below.")
                event = st.dataframe(
                    display,
                    use_container_width=True,
                    hide_index=True,
                    on_select="rerun",
                    selection_mode="single-row",
                    key="continuous_table",
                )
                st.caption(f"{len(display)} meters running continuously all week")
                st.download_button(
                    "Download CSV",
                    display.to_csv(index=False),
                    file_name="continuous_users_this_week.csv",
                )

                selected_rows = event["selection"]["rows"] if event else []
                if selected_rows:
                    sel = filtered.iloc[selected_rows[0]]
                    label = (
                        sel["customer_name"] if pd.notna(sel["customer_name"])
                        else f"acct {sel['account_number'] or '(no account)'}"
                    )
                    st.markdown(f"#### Usage for {label} — meter {sel['meter_number']}")
                    _render_usage_chart(
                        conn, sel["miu_id"], key_prefix="continuous_selected", default_view="Last 7"
                    )

# ------------------------------------------------------------------ Map --
# One combined map instead of two: parcels colored by leak status (from
# meter_parcels, the billing-address-matched set) as the base layer, plus
# the utility's own GPS-surveyed meter locations (gis_meters) as red dots on
# top. Parcels that only the GIS survey matched (no billing/meter_parcels
# link, so no leak data to color them by) are still drawn, in neutral blue,
# so survey coverage doesn't disappear just because it merged into one map.
with tab_map:
    st.subheader("🗺️ Meter map")
    meter_parcels = _cached_meter_parcels(conn)
    gis_meters = _cached_gis_meters(conn)

    if meter_parcels.empty and gis_meters.empty:
        st.info(
            "No parcel or meter-location data yet. Run "
            "`python3 import_gis.py \"<path to parcels.geojson>\"` to load parcel boundaries "
            "and match them to meters, and/or `python3 import_meter_locations.py "
            "\"<path to meter shapefile .zip or .shp>\"` to load surveyed meter locations."
        )
    else:
        total_meters = row_counts["customers"] or 1
        gis_survey_n = int((meter_parcels["match_method"] == "gis_survey").sum()) if not meter_parcels.empty else 0
        caption_bits = []
        if not meter_parcels.empty:
            caption_bits.append(
                f"{len(meter_parcels)} of {total_meters} meters ({len(meter_parcels) / total_meters:.0%}) "
                "are matched to a parcel boundary"
                + (
                    f" — {gis_survey_n} from the GPS meter survey (most reliable), the rest "
                    "geocoded or address-matched from billing addresses"
                    if gis_survey_n else " — geocoded or address-matched from billing addresses"
                )
            )
        if not gis_meters.empty:
            gis_matched = gis_meters["parcel_id"].notna().sum()
            caption_bits.append(
                f"{len(gis_meters)} meters from the utility's own GIS survey are plotted as red dots "
                f"({gis_matched} of them ({gis_matched / len(gis_meters):.0%}) also matched to a parcel "
                "from their surveyed GPS coordinate — meter_id there is the utility's internal asset ID, "
                "not Neptune's miu_id, so those aren't joined to usage data)"
            )
        st.caption(
            ". ".join(caption_bits)
            + " (see `import_gis.py` / `import_meter_locations.py` / the README for how matching works)."
        )

        show_smaller_map = st.toggle(
            "Also flag smaller leaks (5+ gal/hr, instead of just 10+ gal/hr)",
            value=False,
            key="map_show_smaller",
        )
        map_threshold = 5 if show_smaller_map else 10

        # Reads the same precomputed table the Continuous Users tab uses (see
        # _cached_leak_status) instead of re-running the windowed query here.
        map_continuous = _cached_leak_status(conn)
        if map_continuous.empty:
            if not meter_parcels.empty:
                st.info("No current leak status yet — showing parcels with no leak status.")
            leak_by_meter = pd.Series(dtype=float)
        else:
            leak_by_meter = map_continuous.set_index("miu_id")["min_consumption"]

        # PolygonLayer with flat per-ring records, not GeoJsonLayer — the
        # deck.gl runtime Streamlit bundles turned out not to render
        # GeoJsonLayer's nested-properties accessors (get_fill_color=
        # "properties.x") at all in testing, silently or with a parse error
        # depending on syntax. Flat top-level dict keys are the pattern
        # Streamlit's own pydeck examples use, and did render correctly.
        records, all_coords, seen_parcel_ids = [], [], set()

        if not meter_parcels.empty:
            df = meter_parcels.copy()
            df["min_consumption"] = df["meter_id"].map(leak_by_meter)
            df["is_leak"] = df["min_consumption"] >= map_threshold
            df["label"] = df["customer_name"].where(df["customer_name"].notna(), "(no billing match)")
            df["tooltip"] = df.apply(
                lambda r: (
                    f"{r['label']} — meter {r['meter_number']}"
                    + (
                        f" · continuous {r['min_consumption']:.0f} gal/hr"
                        if r["is_leak"] else ""
                    )
                ),
                axis=1,
            )
            df["geom_obj"] = df["geometry"].apply(json.loads)
            for _, row in df.iterrows():
                geom = row["geom_obj"]
                if geom["type"] == "Polygon":
                    rings = [geom["coordinates"][0]]
                elif geom["type"] == "MultiPolygon":
                    rings = [part[0] for part in geom["coordinates"]]
                else:
                    continue
                color = _leak_color(row["min_consumption"] if row["is_leak"] else None, map_threshold)
                for ring in rings:
                    records.append({"polygon": ring, "fill_color": color, "tooltip": row["tooltip"]})
                    all_coords.extend(ring)
                seen_parcel_ids.add(row["parcel_id"])

        if not gis_meters.empty:
            # One ring set per distinct matched parcel, not per meter —
            # several meters commonly share a parcel (e.g. an HOA or
            # multi-unit property) — and skip any parcel already drawn above
            # from meter_parcels, so it isn't rendered twice with two colors
            # stacked on top of each other.
            matched_parcels = (
                gis_meters.dropna(subset=["parcel_id", "parcel_geometry"])
                .drop_duplicates("parcel_id")
            )
            for _, row in matched_parcels.iterrows():
                if row["parcel_id"] in seen_parcel_ids:
                    continue
                geom = json.loads(row["parcel_geometry"])
                if geom["type"] == "Polygon":
                    rings = [geom["coordinates"][0]]
                elif geom["type"] == "MultiPolygon":
                    rings = [part[0] for part in geom["coordinates"]]
                else:
                    continue
                for ring in rings:
                    records.append({
                        "polygon": ring, "fill_color": [70, 130, 220, 40],
                        "tooltip": f"Parcel {row['parcel_id']} (GIS survey only, no billing match)",
                    })
                    all_coords.extend(ring)

        layers = []
        if records:
            layers.append(pdk.Layer(
                "PolygonLayer",
                records,
                get_polygon="polygon",
                get_fill_color="fill_color",
                get_line_color=[80, 80, 80, 150],
                filled=True,
                stroked=True,
                line_width_min_pixels=1,
                pickable=True,
                auto_highlight=True,
            ))

        if not gis_meters.empty:
            gis_meters = gis_meters.copy()
            gis_meters["customer_label"] = gis_meters["customer_name"].where(
                gis_meters["customer_name"].notna(), "(no name on file)"
            )
            gis_meters["tooltip"] = gis_meters.apply(
                lambda r: (
                    f"📍 {r['customer_label']} — meter {r['meter_id']}"
                    + (f" · parcel {r['parcel_id']}" if pd.notna(r["parcel_id"]) else " · no parcel match")
                    + (f" · {r['address']}" if pd.notna(r["address"]) else "")
                ),
                axis=1,
            )
            layers.append(pdk.Layer(
                "ScatterplotLayer",
                gis_meters,
                get_position="[lon, lat]",
                get_fill_color=[220, 20, 60, 200],
                get_radius=6,
                radius_min_pixels=3,
                radius_max_pixels=8,
                pickable=True,
                auto_highlight=True,
            ))
            all_coords.extend(zip(gis_meters["lon"], gis_meters["lat"]))

        # Fits the viewport to the bounding box of everything plotted (i.e.
        # Providence) instead of a hardcoded center/zoom, so the map still
        # frames the town correctly if the parcel/survey set changes.
        # view_proportion drops the farthest 0.5% of points before fitting —
        # a handful of parcels are matched to addresses in other Cache
        # Valley towns (e.g. "269 E CENTER ST" landing ~20mi north, in
        # what's almost certainly Smithfield's parcel data, not
        # Providence's — see import_gis.py's address matching), and without
        # trimming those the whole view zooms out to fit them instead of
        # the town.
        if all_coords:
            view_state = pdk.data_utils.compute_view(all_coords, view_proportion=0.995)
        else:
            view_state = pdk.ViewState(latitude=41.7, longitude=-111.8, zoom=12.5)
        st.pydeck_chart(
            pdk.Deck(
                layers=layers,
                initial_view_state=view_state,
                tooltip={"html": "{tooltip}"},
                map_style=None,
            ),
            width="stretch",
            height=600,
        )
        st.caption(
            f"🟡🟠🔴 currently continuous ≥ {map_threshold} gal/hr this week (darker red = higher rate) "
            "· 🔵 no continuous leak this week (darker blue = GIS-survey-only parcel, no billing match) "
            "· 🔴 dot = surveyed meter location from the utility's GIS survey"
        )

# --------------------------------------------------------- Manage Users --
with tab_users:
    st.subheader("Manage users")
    if not IS_ADMIN:
        _locked_message("Manage Users", "admin")
    else:
        st.caption(
            "Admins can create viewer and admin accounts. Only global accounts "
            "can create other global accounts."
        )
        users = db.list_users(conn)
        users_df = (
            pd.DataFrame(users)[["email", "role", "active", "created_by", "created_at"]]
            if users else pd.DataFrame(columns=["email", "role", "active", "created_by", "created_at"])
        )
        users_df["created_at"] = users_df["created_at"].apply(_fmt_dt)
        st.dataframe(users_df, use_container_width=True, hide_index=True)

        st.markdown("#### Create a user")
        allowed_roles = ["viewer", "admin", "global"] if IS_GLOBAL else ["viewer", "admin"]
        with st.form("create_user", clear_on_submit=True):
            new_email = st.text_input("Email")
            new_password = st.text_input("Temporary password", type="password")
            new_role = st.selectbox("Role", allowed_roles)
            submitted = st.form_submit_button("Create user")
        if submitted:
            if not new_email or "@" not in new_email:
                st.error("Enter a valid email address.")
            elif len(new_password) < 8:
                st.error("Password must be at least 8 characters.")
            elif db.get_user_by_email(conn, new_email):
                st.error("That email already has an account.")
            else:
                db.create_user(
                    conn, new_email, new_password, new_role,
                    created_by=st.session_state.get("email", "local"),
                )
                st.success(f"Created {new_role} account for {new_email}.")
                st.rerun()

        st.markdown("#### Deactivate / reactivate")
        for u in users:
            cols = st.columns([3, 1, 1, 2])
            cols[0].write(u["email"])
            cols[1].write(u["role"])
            cols[2].write("active" if u["active"] else "inactive")
            is_self = u["email"] == st.session_state.get("email")
            is_last_global = (
                u["role"] == "global"
                and u["active"]
                and db.count_users_with_role(conn, "global", active_only=True) <= 1
            )
            disabled = is_self or is_last_global
            label = "Deactivate" if u["active"] else "Reactivate"
            if cols[3].button(label, key=f"toggle_{u['id']}", disabled=disabled):
                db.set_user_active(conn, u["id"], not u["active"])
                st.rerun()
            if is_self:
                cols[3].caption("(you)")
            elif is_last_global:
                cols[3].caption("(last global — can't deactivate)")

# ------------------------------------------------------------- Sync tab --
# Defined as a function (not `with tab_sync:` directly) so the gate below
# can skip the call — locked-out users in PUBLIC_MODE see the tab but never
# execute this code.
def _render_sync_tab():
    st.subheader("Sync from Neptune 360")
    if client is None:
        st.error("Configure .env first (see .env.example).")
    else:
        st.markdown("#### 1. Customers")
        st.caption("Pulls every meter/account record. Usually 1-2 API calls total.")
        if st.button("Sync Customers now"):
            with st.spinner("Pulling endpoints from Neptune..."):
                try:
                    result = sync.sync_customers(client)
                    st.cache_data.clear()
                    st.success(f"Synced {result['customers_synced']} customer/meter records.")
                except (NeptuneAPIError, BudgetExceeded) as e:
                    st.error(str(e))

        st.divider()
        st.markdown("#### 2. Recent water usage (daily incremental sync)")
        st.caption("Pulls the last few days for every meter. Small — safe to run daily.")
        days = st.slider("Days back", 1, 14, 3, key="recent_days")
        if st.button("Sync recent usage now"):
            with st.spinner("Pulling recent consumption..."):
                try:
                    n = sync.sync_water_usage_recent(client, days=days)
                    st.cache_data.clear()
                    st.success(f"Wrote {n} usage rows.")
                except (NeptuneAPIError, BudgetExceeded) as e:
                    st.error(str(e))

        st.divider()
        st.markdown("#### 3. Historical backfill (resumable)")
        st.caption(
            "Consumption calls are capped at 7 days and 100 meters per call, so a full "
            "history pull can take many days of budget. This runs what it can today, "
            "saves its place, and picks up where it left off next time you click it."
        )
        col1, col2 = st.columns(2)
        default_start = date.today() - timedelta(days=730)
        start_date = col1.date_input("Backfill from", default_start, max_value=date.today())
        end_date = col2.date_input("Backfill through", date.today(), max_value=date.today())
        num_meters = row_counts["customers"] or 1
        est = sync.estimate_calls(num_meters, start_date, end_date)
        st.caption(
            f"Estimated total calls for this full range across {num_meters} meters: **{est}**. "
            f"You have **{client.calls_remaining()}** left today, so this will likely take "
            f"about **{max(1, -(-est // max(client.calls_remaining(), 1)))}** sync sessions."
        )
        actual = st.checkbox("Use actual readings only (skip estimated consumption)", value=False)
        if st.button("Run / resume backfill"):
            with st.spinner("Pulling consumption history... this uses your remaining daily budget."):
                try:
                    result = sync.start_or_resume_backfill(client, start_date, end_date, actual)
                    st.cache_data.clear()
                    st.success(
                        f"{result['status']}: completed {result['windows_completed_this_run']} "
                        f"date windows, wrote {result['rows_written_this_run']} rows. "
                        f"Next cursor: {_fmt_dt(result['next_cursor'], with_time=False)}."
                    )
                    if result["status"] == "budget_exceeded":
                        st.info("Daily budget used up — click again after the quota resets to continue.")
                except (NeptuneAPIError, BudgetExceeded) as e:
                    st.error(str(e))

# --------------------------------------------------------------- Ask AI --
def _render_ai_tab():
    st.subheader("Ask AI about your data")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        st.info("Set ANTHROPIC_API_KEY in .env to enable this tab.")
    else:
        question = st.text_input(
            "Ask a question about customers or water usage",
            placeholder="Which meters used more than 5000 gallons last month?",
        )
        if question:
            from anthropic import Anthropic

            ai_client = Anthropic(api_key=api_key)
            model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

            schema_text = (
                "CREATE TABLE customers (miu_id TEXT PRIMARY KEY, site_id TEXT, "
                "account_number TEXT, premise_key TEXT, register_id TEXT, meter_number TEXT, "
                "meter_type TEXT, meter_size TEXT, meter_manufacturer TEXT, dials TEXT, "
                "multiplier TEXT, unit_of_measure TEXT, cycle_route TEXT, synced_at TEXT);\n"
                "CREATE TABLE water_usage (site_id TEXT, miu_id TEXT, meter_number TEXT, "
                "reading_date TEXT, consumption REAL, consumption_with_multiplier REAL, "
                "synced_at TEXT);\n"
                "-- Name/address/phone/email from the billing system (not from Neptune).\n"
                "-- Join to customers/water_usage via meter_id = miu_id.\n"
                "CREATE TABLE customer_billing (meter_id TEXT PRIMARY KEY, account_number TEXT, "
                "customer_name TEXT, location TEXT, location_no TEXT, parcel_id TEXT, "
                "primary_phone TEXT, secondary_phone TEXT, email_address TEXT);"
            )

            with st.spinner("Writing a query..."):
                sql_resp = ai_client.messages.create(
                    model=model,
                    max_tokens=500,
                    system=(
                        "You write a single read-only SQLite SELECT query to answer the "
                        "user's question, given this schema:\n\n" + schema_text +
                        "\n\nRespond with ONLY the SQL in a ```sql fenced code block. "
                        "Never write INSERT/UPDATE/DELETE/DROP/ATTACH/PRAGMA. "
                        "reading_date is an ISO datetime string; use date()/strftime() as needed."
                    ),
                    messages=[{"role": "user", "content": question}],
                )
                sql_text = sql_resp.content[0].text
                m = re.search(r"```sql\s*(.*?)```", sql_text, re.DOTALL) or re.search(
                    r"```\s*(.*?)```", sql_text, re.DOTALL
                )
                sql = (m.group(1) if m else sql_text).strip().rstrip(";")

            if not re.match(r"(?is)^\s*select\b", sql) or re.search(
                r"(?i)\b(insert|update|delete|drop|alter|attach|pragma|create|replace)\b", sql
            ):
                st.error("The generated query wasn't a safe read-only SELECT. Try rephrasing.")
                st.code(sql, language="sql")
            else:
                try:
                    ro_conn = db.get_conn(readonly=True)
                    result_df = pd.read_sql_query(sql, ro_conn)
                except Exception as e:
                    st.error(f"Query failed: {e}")
                    st.code(sql, language="sql")
                else:
                    with st.spinner("Summarizing..."):
                        answer_resp = ai_client.messages.create(
                            model=model,
                            max_tokens=500,
                            messages=[{
                                "role": "user",
                                "content": (
                                    f"Question: {question}\n\nQuery result (CSV, up to 200 rows):\n"
                                    f"{result_df.head(200).to_csv(index=False)}\n\n"
                                    "Answer the question in plain language based on this data."
                                ),
                            }],
                        )
                        st.markdown(answer_resp.content[0].text)
                    with st.expander("Show SQL and raw results"):
                        st.code(sql, language="sql")
                        st.dataframe(result_df, use_container_width=True, hide_index=True)


with tab_sync:
    if not ALLOW_SYNC:
        st.info(
            "🔒 Sync & Backfill is disabled on this machine. It only runs on the production "
            "server, so a local checkout can't silently spend Neptune API quota the server's "
            "own budget tracking doesn't know about. Set `NEPTUNE_ALLOW_SYNC=true` in `.env` "
            "if you really need to run it here (see README)."
        )
    elif IS_GLOBAL:
        _render_sync_tab()
    else:
        _locked_message("Sync & Backfill", "global")

with tab_ai:
    if IS_GLOBAL:
        _render_ai_tab()
    else:
        _locked_message("Ask AI", "global")
