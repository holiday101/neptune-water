"""Thin client for the Neptune 360 SDK REST API.

Every call that counts against the daily quota goes through _call(), which
logs it to api_call_log and refuses to fire once the configured daily budget
is exhausted (raising BudgetExceeded instead of silently spending calls).
"""
import os
import time
from datetime import datetime, timezone

import requests

import neptune_db as db

RETRYABLE_STATUS = {500, 502, 503, 504}
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = (3, 8, 20)


class NeptuneAPIError(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    """Raised instead of making an API call once the daily budget is spent."""


class NeptuneClient:
    def __init__(self, conn=None):
        self.api_key = os.environ["NEPTUNE_API_KEY"]
        self.client_id = os.environ["NEPTUNE_CLIENT_ID"]
        self.client_secret = os.environ["NEPTUNE_CLIENT_SECRET"]
        self.site_id = os.environ["NEPTUNE_SITE_ID"]
        self.host = os.environ.get("NEPTUNE_HOST", "aygpg477xh.execute-api.us-west-2.amazonaws.com")
        self.daily_budget = int(os.environ.get("NEPTUNE_DAILY_CALL_BUDGET", "480"))
        self.base_url = f"https://{self.host}"
        self.conn = conn or db.get_conn()
        self.session = requests.Session()
        self._token = None
        self._token_expiry = 0  # epoch seconds

    # ---- budget -----------------------------------------------------

    def calls_remaining(self):
        return max(0, self.daily_budget - db.calls_today(self.conn))

    def _spend(self, endpoint_name):
        if self.calls_remaining() <= 0:
            raise BudgetExceeded(
                f"Daily call budget ({self.daily_budget}) is used up. Try again after "
                "midnight UTC, or raise NEPTUNE_DAILY_CALL_BUDGET in .env if you're sure "
                "Neptune's actual limit is higher."
            )
        db.record_api_call(self.conn, endpoint_name)

    # ---- auth ---------------------------------------------------------

    def _get_token(self):
        # Reuse cached token until <30s left on its 10-minute life.
        if self._token and time.time() < self._token_expiry - 30:
            return self._token
        self._spend("token")
        resp = self._send(
            "GET",
            f"{self.base_url}/api/v1/token",
            headers={
                "x-api-key": self.api_key,
                "client-id": self.client_id,
                "client-secret": self.client_secret,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise NeptuneAPIError(f"Token request failed: {resp.status_code} {resp.text}")
        data = resp.json()
        self._token = data["AccessToken"]
        self._token_expiry = time.time() + int(data.get("ExpiresIn", 600))
        return self._token

    def _headers(self):
        return {
            "x-api-key": self.api_key,
            "Authorization": f"Bearer {self._get_token()}",
        }

    def _send(self, method, url, **kwargs):
        """requests call with retry on transient 5xx/timeout. Does NOT retry on
        4xx (those are real errors, not worth burning budget re-sending)."""
        last_exc = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = self.session.request(method, url, timeout=kwargs.pop("timeout", 60), **kwargs)
            except requests.exceptions.RequestException as e:
                last_exc = e
                resp = None
            if resp is not None and resp.status_code not in RETRYABLE_STATUS:
                return resp
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS[attempt])
        if resp is not None:
            return resp  # exhausted retries, return the last (bad) response
        raise NeptuneAPIError(f"Request to {url} failed after {MAX_RETRIES} retries: {last_exc}")

    # ---- endpoints (== "customers": meter/account/premise records) ---

    def get_endpoints_page(self, page=1):
        self._spend("endpoints")
        resp = self._send(
            "GET",
            f"{self.base_url}/api/v2/endpoints",
            headers=self._headers(),
            params={"site_id": self.site_id, "page": page},
        )
        if resp.status_code != 200:
            raise NeptuneAPIError(f"/endpoints failed: {resp.status_code} {resp.text}")
        return resp.json()

    def iter_all_endpoints(self):
        """Yields every endpoint dict across all pages (5000/page)."""
        page = 1
        while True:
            data = self.get_endpoints_page(page)
            for ep in data.get("endpoints", []):
                yield ep
            paging = data.get("paging") or {}
            if not paging.get("next") and not paging.get("Next"):
                return
            page += 1

    # ---- consumption (== "water usage") -------------------------------

    def get_consumption_page(self, begin_date, end_date, page=1, actual_consumption=False):
        """begin/end_date: 'YYYY-MM-DD' strings. end_date must be within 7 days of begin_date."""
        self._spend("consumption")
        resp = self._send(
            "GET",
            f"{self.base_url}/api/v1/consumption",
            headers=self._headers(),
            params={
                "site_id": self.site_id,
                "begin_date": begin_date,
                "end_date": end_date,
                "actual_consumption": str(actual_consumption).lower(),
                "page": page,
            },
        )
        if resp.status_code != 200:
            raise NeptuneAPIError(f"/consumption failed: {resp.status_code} {resp.text}")
        return resp.json()

    def iter_all_consumption(self, begin_date, end_date, actual_consumption=False):
        """Yields every endpoint's consumption_history dict for one <=7-day window,
        across all pages (100 endpoints/page)."""
        page = 1
        while True:
            data = self.get_consumption_page(begin_date, end_date, page, actual_consumption)
            for ep in data.get("endpoints", []):
                yield ep
            paging = data.get("paging") or {}
            if not paging.get("next") and not paging.get("Next"):
                return
            page += 1

    def post_consumption(self, miu_ids, begin_date, end_date, actual_consumption=False):
        """Bulk consumption lookup for up to 100 specific miu_ids in one call —
        no pagination needed since the request itself is capped at 100 endpoints."""
        if len(miu_ids) > 100:
            raise ValueError("post_consumption accepts at most 100 miu_ids per call")
        self._spend("consumption_post")
        resp = self._send(
            "POST",
            f"{self.base_url}/api/v1/consumption",
            headers={**self._headers(), "Content-Type": "application/json"},
            json={
                "site_id": self.site_id,
                "miu_ids": list(miu_ids),
                "begin_date": begin_date,
                "end_date": end_date,
                "actual_consumption": actual_consumption,
            },
        )
        if resp.status_code != 200:
            raise NeptuneAPIError(f"POST /consumption failed: {resp.status_code} {resp.text}")
        return resp.json()
