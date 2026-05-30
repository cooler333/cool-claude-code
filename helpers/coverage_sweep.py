#!/usr/bin/env python3
"""coverage_sweep.py — belt-and-suspenders coverage audit (NOT part of the pipeline).

The deterministic pipeline (fetch.py) discovers via *scoped* queries
(topic:claude-code, "claude in:name", ...). That is cheap and precise but can miss
a popular ecosystem repo whose scoped query simply never returns it. This script
takes the opposite, exhaustive angle: sweep EVERY GitHub repo at >= min_stars,
then apply the SAME selection rules fetch.py uses (alias_map -> denylist ->
in_scope -> not archived) and report the in-scope repos that are NOT already in
helpers/repos.json. Those are the misses.

The Search API hard-caps each query at 1,000 results, so the >= min_stars set
(a couple thousand repos) is swept via adaptive star-range buckets that each stay
under 1,000, then concatenated (ranges don't overlap -> already globally sorted).

Read-only audit tool: it writes ONLY to a temp folder + stdout. It never touches
repos.json or README.md, and reuses fetch.py's pure selection helpers so the rules
can't drift. stdlib only; network via ghclient.

Usage:
  python helpers/coverage_sweep.py                  # sweep, write results to a temp dir
  python helpers/coverage_sweep.py --out-dir DIR    # write results to DIR instead
  python helpers/coverage_sweep.py --min-stars N    # override config min_stars
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

from ghclient import GHClient, RateLimitExhausted, get_token
from fetch import in_scope, valid_name, canon, clean_text, log

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"
DEFAULT_REPOS = HERE / "repos.json"

SEARCH_RESULT_CAP = 1000   # GitHub Search API hard ceiling per query
PER_PAGE = 100             # Search API max page size


# --------------------------------------------------------------------------
# sweep
# --------------------------------------------------------------------------

def _search(client: GHClient, q: str, page: int) -> dict:
    url = (f"/search/repositories?q={quote(q)}"
           f"&sort=stars&order=desc&per_page={PER_PAGE}&page={page}")
    return client.get_json(url, klass="search") or {}


def _count(client: GHClient, q: str) -> int:
    return int(_search(client, q, 1).get("total_count", 0))


def _page_all(client: GHClient, q: str, sink: dict) -> int:
    """Page through a query (<1000 results) into sink keyed by lowercased name."""
    added = 0
    for page in range(1, (SEARCH_RESULT_CAP // PER_PAGE) + 1):
        items = _search(client, q, page).get("items") or []
        for it in items:
            full = it.get("full_name") or ""
            if not full:
                continue
            key = full.lower()
            if key not in sink:
                sink[key] = {
                    "full_name": full,
                    "stars": int(it.get("stargazers_count") or 0),
                    "description": clean_text(it.get("description")),
                    "topics": sorted(it.get("topics") or []),
                    "archived": bool(it.get("archived")),
                }
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


# --------------------------------------------------------------------------
# selection (mirrors fetch.py: alias -> denylist -> in_scope -> not archived)
# --------------------------------------------------------------------------

def matched_term(repo: dict, scope: dict) -> str:
    text = " ".join([repo["full_name"].lower(), repo["description"].lower(),
                     *(t.lower() for t in repo["topics"])])
    for term in scope.get("any", []):
        if term.lower() in text:
            return term
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--repos", type=Path, default=DEFAULT_REPOS)
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="dir for results (default: a fresh temp dir)")
    ap.add_argument("--min-stars", type=int, default=None,
                    help="override config min_stars")
    args = ap.parse_args()

    cfg = json.loads(args.config.read_text())
    min_stars = args.min_stars if args.min_stars is not None else cfg["min_stars"]
    scope = cfg.get("scope_filter") or {}
    alias_lc = {k.lower(): v for k, v in cfg.get("alias_map", {}).items()}
    deny_lc = {x.lower() for x in cfg.get("denylist", [])}
    target_count = cfg.get("target_count", 0)

    repos_doc = json.loads(args.repos.read_text())
    listed = repos_doc.get("repos", [])
    have = {canon(alias_lc, r["full_name"]).lower() for r in listed}
    # lowest star count currently published -> a missed repo above it outranks an
    # existing entry (and the list is "partial" anyway, so anything >= min_stars
    # is a candidate for the published top-N).
    listed_min = min((r.get("stars", 0) for r in listed), default=min_stars)
    log(f"current repos.json: {len(have)} repos; min_stars={min_stars}; "
        f"lowest listed stars={listed_min}")

    client = GHClient(get_token(), cfg["http"]["user_agent"])

    # 1) global max star count -> bounded top of the sweep range
    top = _search(client, f"stars:>={min_stars}", 1).get("items") or []
    if not top:
        log("FATAL: sweep returned nothing.")
        return 1
    max_stars = int(top[0].get("stargazers_count") or min_stars)
    grand_total = _count(client, f"stars:>={min_stars}")
    log(f"sweeping {grand_total} repos with >= {min_stars} stars "
        f"(max {max_stars})...")

    # 2) adaptive bucketed sweep
    sink: dict[str, dict] = {}
    sweep(client, min_stars, max_stars, sink)
    swept = sorted(sink.values(),
                   key=lambda r: (-r["stars"], r["full_name"].lower()))
    log(f"swept {len(swept)} unique repos")

    # 3) apply selection rules + find what's missing from repos.json
    missed, denied_hits, in_list = [], 0, 0
    for r in swept:
        full = r["full_name"]
        if not valid_name(full):
            continue
        cn = canon(alias_lc, full)
        if cn.lower() in deny_lc or full.lower() in deny_lc:
            denied_hits += 1
            continue
        if r["archived"] or r["stars"] < min_stars:
            continue
        if not in_scope(r, scope):
            continue
        if cn.lower() in have:
            in_list += 1
            continue
        missed.append({**r, "canonical": cn, "matched_term": matched_term(r, scope)})

    log(f"in-scope & already listed: {in_list} | denylisted (skill exceptions): "
        f"{denied_hits} | MISSED in-scope: {len(missed)}")

    # 4) write results to a temp folder
    out_dir = args.out_dir or Path(tempfile.mkdtemp(prefix="cc-coverage-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "sweep_raw.json").write_text(
        json.dumps({"min_stars": min_stars, "max_stars": max_stars,
                    "grand_total": grand_total, "count": len(swept),
                    "repos": swept}, indent=2, ensure_ascii=False))
    (out_dir / "missed.json").write_text(
        json.dumps({"min_stars": min_stars, "count": len(missed),
                    "target_count": target_count, "repos": missed},
                   indent=2, ensure_ascii=False))

    # 5) human-readable summary to stdout
    print(f"\n# Coverage sweep — {len(missed)} in-scope repos missing from repos.json\n")
    print(f"(swept {len(swept)} repos >= {min_stars} stars; results in {out_dir})\n")
    print(f"{'stars':>8}  {'full_name':40}  matched-term")
    print(f"{'-'*8}  {'-'*40}  {'-'*16}")
    for m in missed:
        flag = "  <- outranks a listed repo" if m["stars"] >= listed_min else ""
        print(f"{m['stars']:>8}  {m['full_name']:40}  {m['matched_term']}{flag}")
    print(f"\nfiles: {out_dir}/sweep_raw.json  {out_dir}/missed.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
