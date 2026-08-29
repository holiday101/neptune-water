"""Sync orchestration: pulls data from Neptune into SQLite, respecting the
daily call budget and resuming multi-day backfills where they left off.
"""
import math
from datetime import date, datetime, timedelta

import neptune_db as db
from neptune_client import BudgetExceeded

WINDOW_SPAN_DAYS = 6  # observed live: API rejects a 7-day *difference*, 6 is the real max
                       # -> each call covers 7 calendar days (begin_date..begin_date+6)
CONSUMPTION_PAGE_SIZE = 100
ENDPOINTS_PAGE_SIZE = 5000

BACKFILL_STATE_KEY = "water_usage_backfill"


def _d(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def date_windows(start, end, span=WINDOW_SPAN_DAYS):
    """Yield (begin, end) date pairs, each covering up to span+1 calendar days,
    tiling [start, end] inclusive."""
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=span), end)
        yield cur, chunk_end
        cur = chunk_end + timedelta(days=1)


def estimate_calls(num_endpoints, start_date, end_date):
    """Rough call-count estimate for a consumption backfill over a date range."""
    num_days = (end_date - start_date).days + 1
    pages = math.ceil(max(num_endpoints, 1) / CONSUMPTION_PAGE_SIZE)
    windows = math.ceil(num_days / (WINDOW_SPAN_DAYS + 1))
    return pages * windows


# ---- customers ----------------------------------------------------------

def sync_customers(client):
    """Full pull of all endpoints (meters/accounts). Usually cheap: ENDPOINTS_PAGE_SIZE
    per call, so most utilities finish in 1-2 calls."""
    rows = []
    for ep in client.iter_all_endpoints():
        rows.append({
            "miu_id": ep.get("miu_id"),
            "site_id": ep.get("site_id"),
            "account_number": ep.get("account_number"),
            "premise_key": ep.get("premise_key"),
            "register_id": ep.get("register_id"),
            "meter_number": ep.get("meter_number"),
            "meter_type": ep.get("meter_type"),
            "meter_size": ep.get("meter_size"),
            "meter_manufacturer": ep.get("meter_manufacturer"),
            "dials": ep.get("dials"),
            "multiplier": ep.get("multiplier"),
            "unit_of_measure": ep.get("unit_of_measure"),
            "cycle_route": ep.get("cycle_route"),
        })
    if rows:
        db.upsert_customers(client.conn, rows)
    return {"customers_synced": len(rows)}


# ---- water usage: recent / incremental -----------------------------------

def sync_water_usage_recent(client, days=3, actual_consumption=False):
    """Pull the last `days` days for every meter. Small, safe for a daily cron:
    e.g. 3000 meters / 100 per page = 30 calls for a single ~8-day window."""
    end = date.today()
    begin = end - timedelta(days=days)
    return _pull_window(client, begin, end, actual_consumption)


def _pull_window(client, begin, end, actual_consumption):
    rows = []
    for ep in client.iter_all_consumption(begin.isoformat(), end.isoformat(), actual_consumption):
        miu_id = ep.get("miu_id")
        meter_number = ep.get("meter_number")
        for h in ep.get("consumption_history", []):
            rows.append({
                "site_id": client.site_id,
                "miu_id": miu_id,
                "meter_number": meter_number,
                "reading_date": h.get("reading_date"),
                "consumption": h.get("consumption"),
                "consumption_with_multiplier": h.get("consumption_with_multiplier"),
            })
    if rows:
        db.upsert_water_usage(client.conn, rows)
    return len(rows)


# ---- water usage: resumable historical backfill --------------------------

def start_or_resume_backfill(client, overall_start, overall_end, actual_consumption=False):
    """Runs consumption backfill for [overall_start, overall_end], persisting
    progress after every window so it can be resumed across days once the
    daily budget runs out. Returns a summary dict.

    overall_start/overall_end: date objects.
    """
    state = db.get_sync_state(client.conn, BACKFILL_STATE_KEY)
    if (
        state
        and state.get("overall_start") == overall_start.isoformat()
        and state.get("overall_end") == overall_end.isoformat()
    ):
        cursor = _d(state["cursor"])
    else:
        cursor = overall_start
        state = {
            "overall_start": overall_start.isoformat(),
            "overall_end": overall_end.isoformat(),
            "cursor": cursor.isoformat(),
        }

    windows_done = 0
    rows_written = 0
    stopped_reason = "complete"

    for win_begin, win_end in date_windows(cursor, overall_end):
        try:
            rows_written += _pull_window(client, win_begin, win_end, actual_consumption)
        except BudgetExceeded:
            state["cursor"] = win_begin.isoformat()  # retry this window next time
            db.set_sync_state(client.conn, BACKFILL_STATE_KEY, state)
            stopped_reason = "budget_exceeded"
            break
        windows_done += 1
        state["cursor"] = (win_end + timedelta(days=1)).isoformat()
        db.set_sync_state(client.conn, BACKFILL_STATE_KEY, state)
    else:
        db.clear_sync_state(client.conn, BACKFILL_STATE_KEY)

    return {
        "windows_completed_this_run": windows_done,
        "rows_written_this_run": rows_written,
        "next_cursor": state["cursor"],
        "overall_end": overall_end.isoformat(),
        "status": stopped_reason,
    }


# ---- ranked deep-dive: best use of a limited call budget ------------------

def rank_and_deep_dive(client, call_budget, history_days=730, recent_scan_days=6,
                        actual_consumption=False):
    """Two-phase strategy for a limited call budget:

    Phase 1 (cheap census): one recent window, all meters, ranks everyone by
    recent usage so we know who's worth spending the expensive calls on.

    Phase 2 (deep dive): full `history_days` history, via the bulk POST
    endpoint (100 meters/call), for as many top-ranked meters as the
    remaining budget allows.

    Returns a summary dict. Never exceeds call_budget (checked before every
    call), and everything written to water_usage/usage_scan is real data,
    not a placeholder.
    """
    calls_start = db.calls_today(client.conn)

    def calls_used():
        return db.calls_today(client.conn) - calls_start

    # ---- Phase 1: rank all meters by recent usage --------------------
    scan_end = date.today()
    scan_begin = scan_end - timedelta(days=recent_scan_days)
    totals = {}  # miu_id -> {"meter_number":..., "total":..., "points":...}
    page = 1
    while True:
        if calls_used() >= call_budget:
            break
        data = client.get_consumption_page(scan_begin.isoformat(), scan_end.isoformat(), page,
                                            actual_consumption)
        eps = data.get("endpoints", [])
        for ep in eps:
            hist = ep.get("consumption_history", [])
            totals[ep.get("miu_id")] = {
                "meter_number": ep.get("meter_number"),
                "total": sum(h.get("consumption_with_multiplier", 0) or 0 for h in hist),
                "points": len(hist),
            }
        paging = data.get("paging") or {}
        if not paging.get("next") and not paging.get("Next"):
            break
        page += 1

    db.upsert_usage_scan(client.conn, [
        {
            "miu_id": miu_id, "meter_number": v["meter_number"],
            "window_begin": scan_begin.isoformat(), "window_end": scan_end.isoformat(),
            "total_consumption": v["total"], "points": v["points"],
        }
        for miu_id, v in totals.items()
    ])
    phase1_calls = calls_used()

    # ---- Phase 2: full-history deep dive on the top-ranked meters -----
    windows = list(date_windows(date.today() - timedelta(days=history_days), date.today()))
    remaining_budget = call_budget - phase1_calls
    batches_feasible = max(0, remaining_budget // max(len(windows), 1))
    candidate_n = batches_feasible * 100

    ranked = sorted(totals.items(), key=lambda kv: kv[1]["total"], reverse=True)
    candidates = [miu_id for miu_id, _ in ranked[:candidate_n]]

    rows_written = 0
    stopped_reason = "complete"
    meters_completed = 0
    for i in range(0, len(candidates), 100):
        batch = candidates[i:i + 100]
        if calls_used() + len(windows) > call_budget:
            stopped_reason = "budget_exceeded_before_batch"
            break
        try:
            for win_begin, win_end in windows:
                data = client.post_consumption(batch, win_begin.isoformat(), win_end.isoformat(),
                                                actual_consumption)
                rows = []
                for ep in data.get("endpoints", []):
                    miu_id = ep.get("miu_id")
                    meter_number = ep.get("meter_number")
                    for h in ep.get("consumption_history", []):
                        rows.append({
                            "site_id": client.site_id, "miu_id": miu_id,
                            "meter_number": meter_number, "reading_date": h.get("reading_date"),
                            "consumption": h.get("consumption"),
                            "consumption_with_multiplier": h.get("consumption_with_multiplier"),
                        })
                if rows:
                    db.upsert_water_usage(client.conn, rows)
                    rows_written += len(rows)
        except BudgetExceeded:
            stopped_reason = "budget_exceeded_mid_batch"
            break
        meters_completed += len(batch)

    return {
        "meters_scanned_phase1": len(totals),
        "calls_used_phase1": phase1_calls,
        "candidates_targeted": len(candidates),
        "meters_completed_phase2": meters_completed,
        "rows_written_phase2": rows_written,
        "history_windows_per_meter": len(windows),
        "total_calls_used": calls_used(),
        "status": stopped_reason,
        "top_candidate_miu_ids": candidates[:20],
    }


def backfill_progress(conn, overall_end=None):
    state = db.get_sync_state(conn, BACKFILL_STATE_KEY)
    if not state:
        return None
    total_days = (_d(state["overall_end"]) - _d(state["overall_start"])).days + 1
    done_days = (_d(state["cursor"]) - _d(state["overall_start"])).days
    return {
        **state,
        "percent_complete": round(100 * max(0, min(done_days, total_days)) / total_days, 1),
    }
