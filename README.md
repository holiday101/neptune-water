# Neptune 360 Data App

Pulls Customers (meter/account records) and Water Usage (consumption history) from
the Neptune 360 SDK into a local SQLite database, with a Streamlit interface to
view both and ask AI questions over the data.

## Setup

```bash
cd /Users/jaredholland/Neptune
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and fill in:
- `NEPTUNE_API_KEY`, `NEPTUNE_CLIENT_ID`, `NEPTUNE_CLIENT_SECRET`, `NEPTUNE_SITE_ID` — from the
  **SDK Access** page in Neptune 360. Never commit `.env` (it's gitignored).
- `ANTHROPIC_API_KEY` — optional, only needed for the "Ask AI" tab.

Run it:

```bash
streamlit run app.py
```

## What data you actually get

Neptune's SDK has no "customer" endpoint with names, addresses, or phone numbers.
The closest thing is `/api/v2/endpoints` — a list of **meters**, each tied to an
`account_number` and `premise_key`. That's what populates the **customers** table
here.

Water usage comes from `/api/v1/consumption` — per-meter consumption history.

Real customer contact info (name, address, phone, email) comes from a **separate
billing-system spreadsheet export**, imported into the **customer_billing** table:

```bash
python3 import_billing.py "/path/to/Customer Listing.xlsx"
```

Re-run this any time you have a fresh export — it upserts, so it's safe to run
repeatedly. It joins to `customers`/`water_usage` on `meter_id == miu_id` (unique
on both sides). Expected columns: `Account No.`, `Customer Name`, `Location`,
`Location No`, `Parcel Id`, `Meter Id`, `Primary Phone`, `Secondary Phone`,
`Email Address` — if your export's headers differ, edit `COLUMN_MAP` in
`import_billing.py`.

If you run this (or make any other direct edit to `data/neptune.db`) while the
Streamlit app is up, restart it afterward — the app caches its main queries in
memory with no expiry (see "Cached queries" in `app.py`) for speed on a large
table, and only invalidates that cache itself when a sync runs through the
Sync tab. A restart clears it too, so the new data shows up immediately
instead of needing a matching in-app sync to trigger the refresh.

## Rate limits — why sync is split into three buttons

Neptune told you 500 calls/day. The API itself layers on top of that:

| Endpoint | Per-call limit |
|---|---|
| `/api/v1/token` | n/a (1 call, token valid 10 min) |
| `/api/v2/endpoints` (customers) | 5,000 meters/page |
| `/api/v1/consumption` (usage) | 100 meters/page, **and** date range capped at 7 days |

Customers sync is cheap (1-2 calls total for most utilities). Water usage is not:
pulling 2 years of history for, say, 3,000 meters is roughly
`ceil(3000/100) pages × ceil(730/8) date-windows ≈ 30 × 92 ≈ 2,760 calls` —
far more than one day's budget.

So:
- **Sync Customers** — run this first, anytime. Cheap.
- **Sync recent usage** — pulls the last few days for every meter. Small, safe to
  run daily (e.g. via cron) to keep the data current going forward.
- **Historical backfill** — resumable. Each click spends whatever's left of today's
  budget, saves its progress (`sync_state` table), and picks up exactly where it
  left off the next time you click it. The Sync tab shows an estimate of total calls
  needed and roughly how many sessions that'll take before you start.

The app tracks every call it makes in `api_call_log` and refuses to exceed
`NEPTUNE_DAILY_CALL_BUDGET` (default 480, a bit under Neptune's stated 500, to leave
headroom). The daily counter resets at UTC midnight — adjust in `neptune_db.py`
(`calls_today`) if Neptune's actual reset is on a different clock; this wasn't
stated in the API docs.

## Files

- `neptune_client.py` — auth + raw API calls, budget-enforced, retries on transient 5xx/timeout
- `neptune_db.py` — SQLite schema, upserts, call log, resumable sync-state store
- `neptune_sync.py` — sync orchestration (customers, recent usage, backfill, ranked deep-dive)
- `import_billing.py` — loads a billing-system spreadsheet export into `customer_billing`
- `import_gis.py` — loads a county parcel GeoJSON export, geocodes billing addresses, and
  matches each meter to a parcel (see "GIS / parcel data" below)
- `app.py` — Streamlit UI (Customers, Water Usage, Sync & Backfill, Ask AI)
- `data/neptune.db` — created on first run, gitignored

## GIS / parcel data

```bash
python3 import_gis.py "/path/to/Parcels.geojson"
```

Expects a GeoJSON `FeatureCollection` of parcel polygons with `PARCEL_ID` and
`PARCEL_ADD` properties (the format Cache County's GIS export uses). Ideally
already in WGS84 (EPSG:4326) — if it's in a projected coordinate system
instead, reproject before importing (`geopandas.GeoDataFrame.to_crs`).

Each billing address is geocoded via the free [Census Bureau batch
geocoder](https://geocoding.geo.census.gov) (no API key needed, ~2,700
addresses in one request) to get real coordinates, then matched to whichever
parcel polygon contains that point. Addresses the geocoder can't place fall
back to normalized address-string matching against the parcel data — this
combination got ~83% of meters matched to a parcel on the first real run,
well above what string-matching alone could do (~59%), since it doesn't
depend on the billing system and the county formatting their addresses the
same way.

Results land in two tables: `parcels` (parcel_id, address, city, geometry as
GeoJSON) and `meter_parcels` (meter_id → parcel_id, plus `match_method` so you
can tell a confident geocoded match from a string-matched fallback one, and
lat/lon for the geocoded ones). Both are fully replaced on each run — re-run
whenever you get a fresh parcel export.

The two hardcoded defaults near the top of the script — `DEFAULT_CITY` and
`DEFAULT_STATE`/`DEFAULT_ZIP`, fed to the geocoder alongside each street
address — assume this utility's accounts are overwhelmingly in one city
(verified against this data: ~1,700 of ~2,000 address matches were
"Providence"). If you import a different utility's billing data, update
those first.

## Ask AI tab

Turns your question into a read-only SQL query (rejects anything that isn't a
plain `SELECT`, and runs it against a connection opened in SQLite's read-only
mode as a second guard), runs it against the local data, then summarizes the
result in plain language. Nothing here calls back out to Neptune — it only reads
what's already been synced.

## Public deployment

Set `NEPTUNE_PUBLIC_MODE=true` to require per-user email/password login before
anyone can see the app at all — for when it's reachable from the open internet
(e.g. `membergolfonline.com/water/`), rather than run locally. Leave it `false`
for local/private use: no login prompt, full access, as if you were the only
user (which you are).

Accounts live in the `app_users` table (`neptune_db.py`), not in `.env` — a
three-tier role ladder:

| Role | Sees |
|---|---|
| `viewer` | Customers, Water Usage, Continuous Users |
| `admin` | viewer, plus **Manage Users** (can create viewer/admin accounts) |
| `global` | everything, including **Sync & Backfill** and **Ask AI** |

All tabs are always visible to everyone logged in; a tab above your role shows
a locked message instead of its real content — logging out and back in with a
higher-role account is the only way to unlock it (there's no in-place upgrade
prompt, since access is per-person now, not a shared secret).

**Bootstrapping your first login**: set `NEPTUNE_SEED_GLOBAL_EMAIL` and
`NEPTUNE_SEED_GLOBAL_PASSWORD` in `.env`. On startup, if no global account
exists yet anywhere in the database, one is created from these. After that
first run they're inert — a password you change later through the app is
never overwritten by them, and deactivating that seed account doesn't
resurrect it either. Safe to leave set indefinitely.

From there, log in as global and use **Manage Users** to create real accounts
for other people — admins can create viewer/admin accounts themselves, but
only a global account can create another global account (prevents a lower-tier
account from escalating itself).

**Security notes**:
- Passwords are stored as salted PBKDF2-HMAC-SHA256 hashes (200,000
  iterations), never in plaintext.
- Failed logins lock that browser session out for 60s after 5 wrong attempts —
  this is *per-session*, so it stops one held-open tab from brute-forcing but
  not a script opening many new sessions. Pair it with a connection-rate limit
  at the reverse proxy (see the `limit_req` example in the nginx config used
  for `membergolfonline.com/water/`) for the other half of that coverage.
- There's no self-service "forgot password" flow (no SMTP configured) — an
  admin/global user resets someone by deactivating their account and creating
  a new one, or you can update `password_hash` directly via the `app_users`
  table if needed.
