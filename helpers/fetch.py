#!/usr/bin/env python3
"""fetch.py — sweep the full >= min_stars GitHub universe to JSON. Network only.

This is the discovery+content step. It does an EXHAUSTIVE star-bucketed sweep of
every public repo at or above `min_stars` (the Search API hard-caps each query at
1,000 results, so the range is split into adaptive star buckets that each stay
under the cap, then concatenated — ranges don't overlap, so the union is already
globally star-sorted). Because the sweep is exhaustive, discovery is complete by
construction: there are no scoped queries that could miss a popular ecosystem repo.

It writes two files:
  - helpers/repos.json       the full universe (every repo >= min_stars), metadata
                             only — the single source of truth for everything else.
  - helpers/out_of_scope.json  the AUTO classification: universe entries that FAIL
                             scope_filter ("not AI/Claude"). Regenerated every run;
                             never hand-edited — tune scope_filter in config.json.
                             Editorial exclusions of in-scope-but-redundant repos
                             live in the human-maintained helpers/filtered.json.

It does NO categorization, brief generation, ranking-to-top-N, README fetching, or
denylist application — those are pure-CPU/editorial steps owned by render.py +
filtered.json, which read this raw snapshot offline.

stdlib only. Network goes through helpers/ghclient.py. Exits non-zero on hard
failure (auth, rate-limit exhaustion, low coverage) so CI never renders/commits
degraded data.

Usage:
  python helpers/fetch.py                 # full sweep -> helpers/repos.json + out_of_scope.json
  python helpers/fetch.py --dry-run       # print both payloads to stdout, don't write
  python helpers/fetch.py --limit 50      # cheap top-slice run (no full sweep, skips floor check)
  python helpers/fetch.py --min-stars N   # override config min_stars
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from ghclient import GHClient, RateLimitExhausted, get_token

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"
DEFAULT_REPOS = HERE / "repos.json"
DEFAULT_OUT_OF_SCOPE = HERE / "out_of_scope.json"

SEARCH_RESULT_CAP = 1000   # GitHub Search API hard ceiling per query
PER_PAGE = 100             # Search API max page size

OWNER_RE = re.compile(r"^[A-Za-z0-9-]+$")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# Strip control chars that are illegal/awkward in single-line text (keep tab/space).
_ILLEGAL = re.compile("[^\x09\x0a\x0d\x20-\U0010ffff]")
_WS = re.compile(r"\s+")


def log(msg: str) -> None:
    sys.stderr.write(msg + "\n")


# --------------------------------------------------------------------------
# pure helpers (no network) — also imported by tests/audits, keep side-effect free
# --------------------------------------------------------------------------

def clean_text(s: str | None) -> str:
    """Collapse whitespace + drop illegal control chars (single-line fields)."""
    if not s:
        return ""
    s = _ILLEGAL.sub("", s)
    return _WS.sub(" ", s).strip()


def valid_name(full: str) -> bool:
    parts = full.split("/")
    if len(parts) != 2:
        return False
    owner, name = parts
    return bool(OWNER_RE.match(owner) and NAME_RE.match(name))


def in_scope(repo: dict, scope: dict) -> bool:
    """Relevance classifier: True if the repo's name/description/topics match any
    ecosystem term. A repo that fails this is recorded in out_of_scope.json."""
    if not scope or not scope.get("require_match"):
        return True
    text = " ".join(
        [repo["full_name"].lower(), repo["description"].lower(),
         *(t.lower() for t in repo["topics"])]
    )
    return any(term.lower() in text for term in scope.get("any", []))


# --------------------------------------------------------------------------
# sweep (network)
# --------------------------------------------------------------------------

def _search(client: GHClient, q: str, page: int) -> dict:
    url = (f"/search/repositories?q={quote(q)}"
           f"&sort=stars&order=desc&per_page={PER_PAGE}&page={page}")
    return client.get_json(url, klass="search") or {}


def _count(client: GHClient, q: str) -> int:
    return int(_search(client, q, 1).get("total_count", 0))


def _add(sink: dict, it: dict) -> bool:
    full = it.get("full_name") or ""
    if not full:
        return False
    key = full.lower()
    if key in sink:
        return False
    sink[key] = {
        "full_name": full,
        "stars": int(it.get("stargazers_count") or 0),
        "description": clean_text(it.get("description")),
        "topics": sorted(it.get("topics") or []),
        "archived": bool(it.get("archived")),
    }
    return True


def _page_all(client: GHClient, q: str, sink: dict) -> int:
    """Page through a query (<1000 results) into sink keyed by lowercased name."""
    added = 0
    for page in range(1, (SEARCH_RESULT_CAP // PER_PAGE) + 1):
        items = _search(client, q, page).get("items") or []
        for it in items:
            if _add(sink, it):
                added += 1
        if len(items) < PER_PAGE:
            break
    return added


def sweep(client: GHClient, lo: int, hi: int, sink: dict) -> None:
    """Adaptively bucket [lo, hi] (inclusive) so each query stays under the
    1,000-result cap, then page each bucket. High ranges first (descending)."""
    q = f"stars:{lo}..{hi}"
    total = _count(client, q)
    if total == 0:
        return
    if total < SEARCH_RESULT_CAP:
        n = _page_all(client, q, sink)
        log(f"  {q}: {total} repos (+{n})")
        return
    if lo >= hi:
        # >=1000 repos sharing one star value: unsplittable, page the first 1000.
        n = _page_all(client, q, sink)
        log(f"  {q}: {total} repos at a single star value; capped at {n}")
        return
    mid = (lo + hi) // 2
    sweep(client, mid + 1, hi, sink)   # higher stars first
    sweep(client, lo, mid, sink)


def cheap_top(client: GHClient, min_stars: int, limit: int, sink: dict) -> None:
    """Cheap test path: just the top `limit` repos by stars, no exhaustive
    bucketing (a handful of Search calls instead of dozens)."""
    q = f"stars:>={min_stars}"
    for page in range(1, (SEARCH_RESULT_CAP // PER_PAGE) + 1):
        items = _search(client, q, page).get("items") or []
        for it in items:
            _add(sink, it)
            if len(sink) >= limit:
                return
        if len(items) < PER_PAGE:
            return


# --------------------------------------------------------------------------
# serialize
# --------------------------------------------------------------------------

def build_repos_payload(repos: list[dict], min_stars: int, generated_at: str) -> dict:
    return {
        "generated_at": generated_at,
        "generator": "helpers/fetch.py",
        "schema_version": 3,
        "min_stars": min_stars,
        "count": len(repos),
        "repos": [
            {
                "full_name": r["full_name"],
                "stars": r["stars"],
                "description": r["description"],
                "topics": r["topics"],
                "archived": r["archived"],
            }
            for r in repos
        ],
    }


def build_out_of_scope_payload(entries: list[dict], min_stars: int, generated_at: str) -> dict:
    return {
        "description": (
            "Auto-generated by helpers/fetch.py: repos >= min_stars that FAIL "
            "scope_filter — classified as outside the Claude Code / agent "
            "ecosystem. Regenerated every run; DO NOT hand-edit (tune scope_filter "
            "in config.json). Editorial exclusions of in-scope-but-redundant repos "
            "live in helpers/filtered.json."
        ),
        "generated_at": generated_at,
        "min_stars": min_stars,
        "count": len(entries),
        "out_of_scope": [
            {
                "repo_id": r["full_name"],
                "stars": r["stars"],
                "description": r["description"],
                "tags": r["topics"],
            }
            for r in entries
        ],
    }


def write_atomic(payload: dict, out: Path) -> None:
    tmp = out.with_suffix(out.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(out)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--out", type=Path, default=DEFAULT_REPOS)
    ap.add_argument("--out-of-scope", type=Path, default=DEFAULT_OUT_OF_SCOPE)
    ap.add_argument("--min-stars", type=int, default=None, help="override config min_stars")
    ap.add_argument("--limit", type=int, default=None,
                    help="cheap top-slice run (skips full sweep + floor check)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = json.loads(args.config.read_text())
    min_stars = args.min_stars if args.min_stars is not None else cfg["min_stars"]
    scope = cfg.get("scope_filter") or {}
    floor = cfg["fetch"]["floor_fraction"]
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    client = GHClient(get_token(), cfg["http"]["user_agent"])

    sink: dict[str, dict] = {}

    if args.limit is not None:
        log(f"cheap run: top {args.limit} repos >= {min_stars} stars (no full sweep)...")
        cheap_top(client, min_stars, args.limit, sink)
    else:
        # bound the sweep by the current global max star count
        top = _search(client, f"stars:>={min_stars}", 1).get("items") or []
        if not top:
            log("FATAL: sweep returned nothing.")
            return 1
        max_stars = int(top[0].get("stargazers_count") or min_stars)
        grand_total = _count(client, f"stars:>={min_stars}")
        log(f"sweeping {grand_total} repos with >= {min_stars} stars (max {max_stars})...")
        sweep(client, min_stars, max_stars, sink)

    # drop invalid owner/name shapes, then sort by stars desc (stable diffs)
    repos = [r for r in sink.values() if valid_name(r["full_name"])]
    repos.sort(key=lambda r: (-r["stars"], r["full_name"].lower()))
    log(f"swept {len(repos)} unique repos")

    # floor check (skip for explicit --limit test runs): a sweep that returns far
    # fewer than the live total means a systemic fetch failure (rate limit) —
    # refuse to overwrite good data with a degraded snapshot.
    if args.limit is None:
        if not repos:
            log("FATAL: zero repos resolved.")
            return 1
        coverage = len(repos) / grand_total if grand_total else 1.0
        log(f"  coverage {coverage:.2%} of {grand_total} live repos")
        if coverage < floor:
            log(f"FATAL: sweep coverage {coverage:.2%} < floor {floor:.0%}; "
                f"refusing to overwrite {args.out.name}.")
            return 1

    out_of_scope = [r for r in repos if not in_scope(r, scope)]
    log(f"  in-scope: {len(repos) - len(out_of_scope)} | out-of-scope (auto): "
        f"{len(out_of_scope)}")

    repos_payload = build_repos_payload(repos, min_stars, generated_at)
    oos_payload = build_out_of_scope_payload(out_of_scope, min_stars, generated_at)

    if args.dry_run:
        sys.stdout.write("=== repos.json ===\n")
        sys.stdout.write(json.dumps(repos_payload, indent=2, ensure_ascii=False) + "\n")
        sys.stdout.write("=== out_of_scope.json ===\n")
        sys.stdout.write(json.dumps(oos_payload, indent=2, ensure_ascii=False) + "\n")
        log(f"(dry-run) {len(repos)} repos; not written")
        return 0

    write_atomic(repos_payload, args.out)
    write_atomic(oos_payload, args.out_of_scope)
    log(f"wrote {args.out} ({len(repos)} repos) + {args.out_of_scope} "
        f"({len(out_of_scope)} out-of-scope)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RateLimitExhausted as e:
        log(f"FATAL: rate limit exhausted ({e}); not writing.")
        sys.exit(2)
