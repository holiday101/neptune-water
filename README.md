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
  run daily (e.g. via cron) to keep the data current going forward. On the deployed
  server this also fires automatically once whenever someone logs in (throttled to
  once per 30 minutes across everyone, not per person — see "Keeping data fresh
  automatically" below) — the daily cron keeps things current overnight, this just
  tops it up during the day.
- **Historical backfill** — resumable, and on the server this now runs itself on a
  daily timer (see "One-time historical backfill" below) instead of needing someone
  to click the button every day until it's done. The button in the Sync tab still
  works too, if you want to nudge it along faster than once a day. Each run spends
  whatever's left of today's budget, saves its progress (`sync_state` table), and
  picks up exactly where it left off next time (button click or cron run alike). The
  Sync tab shows an estimate of total calls needed and roughly how many sessions
  that'll take.

The app tracks every call it makes in `api_call_log` and refuses to exceed
`NEPTUNE_DAILY_CALL_BUDGET` (default 480, a bit under Neptune's stated 500, to leave
headroom). The daily counter resets at UTC midnight — adjust in `neptune_db.py`
(`calls_today`) if Neptune's actual reset is on a different clock; this wasn't
stated in the API docs.

**This tracking is per-database, not per-Neptune-site** — Neptune's real 500/day
cap is shared across every machine hitting the same site's credentials, but
`api_call_log` only sees calls made against *this* `neptune.db`. Two checkouts
syncing against the same site (e.g. your laptop and the production server) can
each think they have budget left while actually racing each other against
Neptune's real quota. That's why Sync & Backfill is gated behind
`NEPTUNE_ALLOW_SYNC` (default `false` — see "Public deployment" below): leave it
off on every checkout except whichever one is actually meant to sync.

### Keeping data fresh automatically

Two unattended mechanisms, both only active where `NEPTUNE_ALLOW_SYNC=true`
(the production server, not a local checkout):

- **`sync_daily.py`** — a standalone script, unrelated to the app process,
  meant to be run by cron/systemd once a day (the server runs it via
  `neptune-sync.timer`, 05:00 MDT). Pulls customers + the last 3 days of
  usage.
- **Login-triggered top-up** — `app.py` also pulls the last 3 days of usage
  once per session, right after someone logs in, throttled to once per 30
  minutes *across every session*, not per person (tracked in `sync_state`,
  key `auto_recent_usage_sync`) — so a wave of logins in the same few
  minutes fires one real sync, not one each. This is what keeps the data
  from going stale during the day between cron runs; it isn't a substitute
  for the cron job (which also refreshes `customers`, which this doesn't).

### One-time historical backfill

```bash
python3 backfill_once.py
```

Wraps the same resumable `start_or_resume_backfill()` the Sync tab's button
uses, but meant to run once a day, unattended, until the whole ~2-year range
is done — then every run after that is a no-op (checks a `sync_state` done
marker first, prints "already completed", exits immediately). Picks up
whatever backfill is already in progress (matches on the exact date range
already stored in `sync_state`, same as the button) rather than starting
over, so switching from clicking the button to cron-driving this script
loses no progress.

On the server this runs via `neptune-backfill.timer` (05:30 MDT, after
`neptune-sync.timer`'s 05:00 recent-usage pull) — set up once with:

```bash
# /etc/systemd/system/neptune-backfill.service
[Unit]
Description=Neptune 360 one-time historical backfill (resumable, no-ops once done)
After=network.target

[Service]
Type=oneshot
User=ubuntu
WorkingDirectory=/home/ubuntu/neptune-water
ExecStart=/home/ubuntu/neptune-water/.venv/bin/python3 backfill_once.py

# /etc/systemd/system/neptune-backfill.timer
[Unit]
Description=Run the Neptune historical backfill once a day until it's done

[Timer]
OnCalendar=*-*-* 05:30:00
RandomizedDelaySec=300
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now neptune-backfill.timer
```

Check progress any time with `journalctl -u neptune-backfill.service` or by
querying `sync_state` for the `water_usage_backfill` key (cursor position)
or `water_usage_backfill_once_done` (set once it's finished).

## Files

- `neptune_client.py` — auth + raw API calls, budget-enforced, retries on transient 5xx/timeout
- `neptune_db.py` — SQLite schema, upserts, call log, resumable sync-state store
- `neptune_sync.py` — sync orchestration (customers, recent usage, backfill, ranked deep-dive)
- `sync_daily.py` — standalone daily cron script: customers + last 3 days of usage
- `backfill_once.py` — standalone daily cron script: resumable historical backfill,
  no-ops once complete (see "One-time historical backfill" below)
- `import_billing.py` — loads a billing-system spreadsheet export into `customer_billing`
- `import_gis.py` — loads a county parcel GeoJSON export, geocodes billing addresses, and
  matches each meter to a parcel (see "GIS / parcel data" below)
- `import_meter_locations.py` — loads the utility's own surveyed meter-location shapefile,
  matches it to parcels directly, and upgrades `meter_parcels` with it (see below)
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

### Upgrading matches with a surveyed meter-location shapefile

If you can get the utility's own meter-inventory GIS export (a shapefile
with one point per physical meter, GPS-surveyed in the field — not
geocoded from an address), it's more reliable than the billing-address
matching above and worth importing on top of it:

```bash
python3 import_meter_locations.py "/path/to/WATER_Meter.zip"
```

Expects the fields Cache County/Providence's utility software exports:
`MeterID`, `Lat`, `Long`, `LocationAd`, `CustomerNa`, plus a few others
(see `load_meters()` in the script). Accepts a `.zip` of the four shapefile
parts (`.shp`/`.shx`/`.dbf`/`.prj`) or a direct `.shp` path.

Since these coordinates are real GPS points rather than geocoded addresses,
matching to a parcel is a direct point-in-polygon join — no geocoder
round-trip needed. Meters installed at the curb/property line (common)
land just outside their parcel's polygon; a small nearest-parcel fallback
(within 20m) catches those. On the first real run this got 100% of 2,476
surveyed meters matched to a parcel.

The script then reconciles this against Neptune's meters too: it
normalized-address-matches `customer_billing` against the survey and, for
every unique match, upserts a `gis_survey` row into `meter_parcels` —
trusted over an existing geocoded/address-string match, not just used as a
fallback, since it's grounded in a physical survey rather than a geocoder's
guess. On the first real run this raised `meter_parcels` coverage from 83%
to 97%, added matches for meters billing-address matching had missed
entirely, and corrected several hundred cases where the geocoded parcel's
own address didn't actually match the billing address.

Results land in `gis_meters` (meter_id, lat, lon, address, customer_name,
parcel_id, match_method — `within` or `nearest`, plus `match_dist_m`),
fully replaced each run. Re-run whenever you get a fresh export.

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

**Sync & Backfill needs a second flag, independent of role**: even a `global`
account sees a "disabled on this machine" message there unless
`NEPTUNE_ALLOW_SYNC=true` is also set (default `false` — see "Rate limits"
above for why). Only set it `true` in the one deployment that should actually
be hitting the Neptune API — normally the production server, not a local
checkout, and not more than one place at a time.

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
