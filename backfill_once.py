"""One-time historical water-usage backfill, meant to run unattended via a
daily cron/systemd timer until it finishes, then become a costless no-op
forever after — unlike the Sync tab's "Run / resume backfill" button, which
needs someone to come back and click it again every day the budget runs out
(which for a full 2-year history, it will, for several days).

Reuses whatever backfill is already in progress (same overall_start/
overall_end already stored in sync_state — see start_or_resume_backfill in
neptune_sync.py, which resumes only on an exact range match) instead of
starting over. If nothing's in progress yet, starts a fresh BACKFILL_DAYS
run ending today.

Requires the same .env as app.py (Neptune credentials) and, like the Sync
tab, only really makes sense on the one deployment that owns this site's
API quota — see NEPTUNE_ALLOW_SYNC in the README. This script doesn't check
that flag itself (it's meant to be wired into cron deliberately, once, by
someone who's already made that call), but honors the same daily call
budget everything else does.

Usage:  python3 backfill_once.py
Exit code 0 whether it made progress, hit today's budget cap, or was
already complete — non-zero only on a real error (bad .env, API failure),
so a cron/systemd log stays quiet on the expected path.
"""
import sys
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv

import neptune_db as db
import neptune_sync as sync
from neptune_client import BudgetExceeded, NeptuneAPIError, NeptuneClient

load_dotenv()

BACKFILL_DAYS = 730  # ~2 years, matching the Sync tab's default range
DONE_MARKER_KEY = "water_usage_backfill_once_done"
RANGE_KEY = "water_usage_backfill_once_range"


def main():
    conn = db.get_conn()

    done = db.get_sync_state(conn, DONE_MARKER_KEY)
    if done:
        print(f"Backfill already completed at {done['completed_at']} — nothing to do.")
        return

    # Continue whatever range is already in sync_state (e.g. from prior
    # manual clicks in the Sync tab) if one exists, so this doesn't discard
    # real progress and start over. Otherwise pick a fresh range now and
    # persist it — NOT date.today() recomputed on every run, since
    # start_or_resume_backfill only resumes on an exact overall_end match;
    # a moving target would never converge on "complete".
    existing_backfill = db.get_sync_state(conn, sync.BACKFILL_STATE_KEY)
    range_state = db.get_sync_state(conn, RANGE_KEY)
    if existing_backfill:
        overall_start = date.fromisoformat(existing_backfill["overall_start"])
        overall_end = date.fromisoformat(existing_backfill["overall_end"])
    elif range_state:
        overall_start = date.fromisoformat(range_state["start"])
        overall_end = date.fromisoformat(range_state["end"])
    else:
        overall_end = date.today()
        overall_start = overall_end - timedelta(days=BACKFILL_DAYS)
    db.set_sync_state(conn, RANGE_KEY, {"start": overall_start.isoformat(), "end": overall_end.isoformat()})

    try:
        client = NeptuneClient(conn=conn)
    except KeyError as e:
        print(f"Missing required setting in .env: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        result = sync.start_or_resume_backfill(client, overall_start, overall_end)
    except (NeptuneAPIError, BudgetExceeded) as e:
        print(f"Backfill run failed: {e}", file=sys.stderr)
        sys.exit(1)

    print(
        f"{result['status']}: {result['windows_completed_this_run']} windows, "
        f"{result['rows_written_this_run']} rows written this run. "
        f"Next cursor: {result['next_cursor']} / {result['overall_end']}."
    )

    if result["status"] == "complete":
        db.set_sync_state(conn, DONE_MARKER_KEY, {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "overall_start": overall_start.isoformat(),
            "overall_end": overall_end.isoformat(),
        })
        print("BACKFILL COMPLETE.")


if __name__ == "__main__":
    main()
