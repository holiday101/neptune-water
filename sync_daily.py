"""Standalone daily sync: pulls customers + last few days of water usage.

Not part of the Streamlit app — meant to be run by a scheduler (systemd timer
or cron) on a server so data stays fresh without anyone clicking the Sync tab.
Requires the same .env as app.py (Neptune credentials).

Run with:  python3 sync_daily.py
"""
import sys

from dotenv import load_dotenv

import neptune_db as db
import neptune_sync as sync
from neptune_client import BudgetExceeded, NeptuneAPIError, NeptuneClient

load_dotenv()


def main():
    conn = db.get_conn()
    try:
        client = NeptuneClient(conn=conn)
    except KeyError as e:
        print(f"Missing required setting in .env: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        result = sync.sync_customers(client)
        print(f"Customers: synced {result['customers_synced']} records.")
    except (NeptuneAPIError, BudgetExceeded) as e:
        print(f"Customers sync failed: {e}", file=sys.stderr)

    try:
        n = sync.sync_water_usage_recent(client, days=3)
        print(f"Water usage: wrote {n} rows.")
    except (NeptuneAPIError, BudgetExceeded) as e:
        print(f"Water usage sync failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
