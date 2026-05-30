"""Shared GitHub API client: auth, per-endpoint rate-limit pacing, backoff.

stdlib only (urllib, json, subprocess, time, random). Used by fetch.py.
render.py does NOT import this — it must stay network-free.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
GRAPHQL = "https://api.github.com/graphql"

# Minimum seconds between calls of each class, to stay under GitHub's limits
# without ever hammering the API. Search: 30/min, Code search: 10/min.
_MIN_INTERVAL = {
    "core": 0.8,
    "search": 2.2,
    "code_search": 6.5,
    "graphql": 1.0,
}

_MAX_RETRIES = 5
_BACKOFF_BASE = 1.0
_BACKOFF_CAP = 60.0
_TIMEOUT = 30


def get_token() -> str:
    """GITHUB_TOKEN / GH_TOKEN env, else `gh auth token`. Empty string if none."""
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        tok = os.environ.get(var)
        if tok:
            return tok.strip()
    try:
        out = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    return ""


class RateLimitExhausted(RuntimeError):
    pass


class GHClient:
    def __init__(self, token: str, user_agent: str):
        if not token:
            raise RuntimeError(
                "No GitHub token. Set GITHUB_TOKEN or run `gh auth login`."
            )
        self.token = token
        self.user_agent = user_agent
        self._last_call: dict[str, float] = {}

    # -- low level ----------------------------------------------------------

    def _throttle(self, klass: str) -> None:
        interval = _MIN_INTERVAL.get(klass, 0.8)
        last = self._last_call.get(klass)
        if last is not None:
            wait = interval - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_call[klass] = time.monotonic()

    def _headers(self, accept: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": self.user_agent,
        }

    def _sleep_backoff(self, attempt: int, retry_after: str | None) -> None:
        if retry_after:
            try:
                time.sleep(int(retry_after) + 1)
                return
            except ValueError:
                pass
        delay = min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** attempt)) + random.uniform(0, 1.0)
        time.sleep(delay)

    def _maybe_wait_for_reset(self, headers) -> None:
        """If primary budget is exhausted, sleep until the reset epoch."""
        if headers.get("X-RateLimit-Remaining") == "0":
            reset = headers.get("X-RateLimit-Reset")
            if reset:
                try:
                    wait = int(reset) - int(time.time()) + 2
                    if 0 < wait <= 3600:
                        sys.stderr.write(f"  rate-limit reached; sleeping {wait}s\n")
                        time.sleep(wait)
                except ValueError:
                    pass

    def _do(self, url, klass, method="GET", body=None, accept="application/vnd.github+json"):
        """Returns (status, headers, raw_bytes). Retries transient failures."""
        for attempt in range(_MAX_RETRIES + 1):
            self._throttle(klass)
            req = urllib.request.Request(url, method=method, headers=self._headers(accept))
            if body is not None:
                req.data = body
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                    data = resp.read()
                    self._maybe_wait_for_reset(resp.headers)
                    return resp.status, resp.headers, data
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return 404, e.headers, b""
                if e.code in (403, 429):
                    if attempt == _MAX_RETRIES:
                        raise RateLimitExhausted(f"{e.code} on {url}") from e
                    self._maybe_wait_for_reset(e.headers)
                    self._sleep_backoff(attempt, e.headers.get("Retry-After"))
                    continue
                if 500 <= e.code < 600:
                    if attempt == _MAX_RETRIES:
                        raise
                    self._sleep_backoff(attempt, None)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError):
                if attempt == _MAX_RETRIES:
                    raise
                self._sleep_backoff(attempt, None)
        raise RateLimitExhausted(f"exhausted retries on {url}")

    # -- public helpers -----------------------------------------------------

    def get_json(self, path: str, klass: str = "core"):
        """GET an API path (relative to API root or absolute). None on 404."""
        url = path if path.startswith("http") else f"{API}{path}"
        status, _, data = self._do(url, klass)
        if status == 404:
            return None
        return json.loads(data) if data else None

    def get_raw(self, path: str, klass: str = "core") -> str | None:
        """GET raw bytes (e.g. content API CSV / README). None on 404."""
        url = path if path.startswith("http") else f"{API}{path}"
        status, _, data = self._do(url, klass, accept="application/vnd.github.raw")
        if status == 404:
            return None
        return data.decode("utf-8", errors="replace")

    def graphql(self, query: str) -> dict:
        """POST a GraphQL query. Backs off on RATE_LIMITED (HTTP 200 + errors)."""
        body = json.dumps({"query": query}).encode("utf-8")
        for attempt in range(_MAX_RETRIES + 1):
            _, _, data = self._do(GRAPHQL, "graphql", method="POST", body=body)
            payload = json.loads(data) if data else {}
            errors = payload.get("errors") or []
            if any(e.get("type") == "RATE_LIMITED" for e in errors):
                if attempt == _MAX_RETRIES:
                    raise RateLimitExhausted("GraphQL RATE_LIMITED")
                self._sleep_backoff(attempt, None)
                continue
            return payload
        raise RateLimitExhausted("GraphQL retries exhausted")
