#!/usr/bin/env python3
"""fetch.py — discover, rank, and serialize the top Claude-Code/AI repos to JSON.

Network-only + selection. Pipeline: discover candidates (repo search + `gh` code
search + awesome-list) -> normalize/dedupe/alias/deny -> validate -> authoritative
metadata via batched GraphQL -> REST fallback for renames -> re-dedupe -> filter
(min_stars + scope + not archived) -> rank + trim -> fetch raw README for the
final-set repos that have no description -> deterministic atomic JSON write.

This script does NO categorization or brief generation — those are pure-CPU steps
owned by helpers/render.py, which reads the raw snapshot offline. fetch.py only
saves raw context (metadata + raw README text).

stdlib only. Network goes through helpers/ghclient.py. Exits non-zero on hard
failure so CI never renders/commits degraded data.

Usage:
  python helpers/fetch.py                 # full run -> helpers/repos.json
  python helpers/fetch.py --dry-run       # print JSON to stdout, don't write
  python helpers/fetch.py --limit 20      # small run (caps candidates, skips floor check)
  python helpers/fetch.py --no-readme     # skip raw-README fetch for empty-description repos
"""

from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from ghclient import GHClient, RateLimitExhausted, get_token

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"
DEFAULT_OUT = HERE / "repos.json"

OWNER_RE = re.compile(r"^[A-Za-z0-9-]+$")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# Strip control chars that are illegal/awkward in text (keep tab/newline/CR).
_ILLEGAL = re.compile("[^\x09\x0a\x0d\x20-\U0010ffff]")
_WS = re.compile(r"\s+")


def log(msg: str) -> None:
    sys.stderr.write(msg + "\n")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def clean_text(s: str | None) -> str:
    """Collapse whitespace + drop illegal control chars (for single-line fields)."""
    if not s:
        return ""
    s = _ILLEGAL.sub("", s)
    return _WS.sub(" ", s).strip()


def clean_multiline(s: str | None) -> str:
    """Drop illegal control chars but PRESERVE newlines (for raw README text)."""
    if not s:
        return ""
    return _ILLEGAL.sub("", s)


def valid_name(full: str) -> bool:
    parts = full.split("/")
    if len(parts) != 2:
        return False
    owner, name = parts
    return bool(OWNER_RE.match(owner) and NAME_RE.match(name))


def canon(alias_map_lc: dict[str, str], full: str) -> str:
    """Apply alias map (case-insensitive); return canonical owner/name."""
    return alias_map_lc.get(full.lower(), full)


def deny_id(entry) -> str:
    """A denylist entry is {"repo_id": ..., "reason": ...}; tolerate a bare
    string for backward compatibility. Returns the owner/name."""
    return entry["repo_id"] if isinstance(entry, dict) else entry


def in_scope(repo: dict, scope: dict) -> bool:
    """Relevance gate: loose search pulls in unrelated giants (awesome lists,
    roadmaps). Keep only repos whose name/description/topics match an ecosystem
    term."""
    if not scope or not scope.get("require_match"):
        return True
    text = " ".join(
        [repo["full_name"].lower(), repo["description"].lower(),
         *(t.lower() for t in repo["topics"])]
    )
    return any(term.lower() in text for term in scope.get("any", []))


def add_candidate(candidates: dict, full: str, source: str) -> None:
    full = full.strip().strip("/")
    if "/" not in full:
        return
    key = full.lower()
    rec = candidates.get(key)
    if rec is None:
        candidates[key] = {"full_name": full, "sources": {source}}
    else:
        rec["sources"].add(source)


# --------------------------------------------------------------------------
# discovery signals
# --------------------------------------------------------------------------

def discover_repo_search(client: GHClient, cfg: dict, candidates: dict) -> None:
    d = cfg["discovery"]
    fetchcfg = cfg["fetch"]
    per_page = fetchcfg["per_query_page_size"]
    max_pages = fetchcfg["max_repo_search_pages"]
    for q in d["repo_search_queries"]:
        for page in range(1, max_pages + 1):
            url = (
                f"/search/repositories?q={quote(q)}"
                f"&sort=stars&order=desc&per_page={per_page}&page={page}"
            )
            try:
                res = client.get_json(url, klass="search")
            except RateLimitExhausted:
                raise
            except Exception as e:  # noqa: BLE001 - best-effort signal
                log(f"  repo_search '{q}' p{page} failed: {e}")
                break
            items = (res or {}).get("items") or []
            for it in items:
                add_candidate(candidates, it.get("full_name", ""), "repo_search")
            if len(items) < per_page:
                break


# Code search is a separate, very low budget (~10/min) and `gh` only surfaces a
# 429 as a generic rc=1, so we sniff its stderr for rate-limit signals and back off
# rather than dropping the query like a permanent failure. Mirrors ghclient's policy.
_CS_PACE = 6.5            # seconds between code-search calls (respect ~10/min)
_CS_MAX_RETRIES = 4
_CS_BACKOFF_BASE = 4.0
_CS_BACKOFF_CAP = 60.0
_CS_RATE_LIMIT_RE = re.compile(
    r"429|rate limit|secondary rate|abuse detection|too many requests",
    re.IGNORECASE,
)


def discover_code_search(cfg: dict, candidates: dict) -> None:
    """Code search via `gh` (newer code-search API; raw REST 422s on bare qualifiers).

    Retries 429/secondary-rate-limit responses with backoff instead of dropping the
    query; genuine errors are logged and skipped after the first attempt.
    """
    for fn in cfg["discovery"].get("code_search_filenames", []):
        for attempt in range(_CS_MAX_RETRIES + 1):
            time.sleep(_CS_PACE)  # pace BEFORE every call, including retries
            try:
                out = subprocess.run(
                    ["gh", "search", "code", "--filename", fn,
                     "--json", "repository", "--limit", "100"],
                    capture_output=True, text=True, timeout=120,
                )
            except (FileNotFoundError, subprocess.SubprocessError) as e:
                log(f"  code_search '{fn}' skipped (gh unavailable: {e})")
                return  # gh missing/broken: no point trying the other filenames
            if out.returncode == 0:
                break
            stderr = out.stderr.strip()
            if _CS_RATE_LIMIT_RE.search(stderr) and attempt < _CS_MAX_RETRIES:
                delay = min(_CS_BACKOFF_CAP, _CS_BACKOFF_BASE * (2 ** attempt))
                delay += random.uniform(0, 2.0)
                log(f"  code_search '{fn}' rate-limited (gh rc={out.returncode}); "
                    f"retry {attempt + 1}/{_CS_MAX_RETRIES} in {delay:.0f}s")
                time.sleep(delay)
                continue
            log(f"  code_search '{fn}' skipped (gh rc={out.returncode}): "
                f"{stderr[:200]}")
            break
        else:
            continue  # retries exhausted on this filename; move to the next
        if out.returncode != 0:
            continue  # broke out on a non-rate-limit error; nothing to parse
        try:
            rows = json.loads(out.stdout or "[]")
        except json.JSONDecodeError:
            continue
        for row in rows:
            repo = row.get("repository") or {}
            name = repo.get("nameWithOwner") or repo.get("fullName") or ""
            add_candidate(candidates, name, "code_search")


def discover_awesome_lists(client: GHClient, cfg: dict, candidates: dict) -> None:
    for entry in cfg["discovery"].get("awesome_lists", []):
        path = f"/repos/{entry['repo']}/contents/{entry['path']}"
        try:
            raw = client.get_raw(path, klass="core")
        except RateLimitExhausted:
            raise
        except Exception as e:  # noqa: BLE001
            log(f"  awesome_list {entry['repo']} failed: {e}")
            continue
        if not raw:
            continue
        col = entry.get("owner_repo_col", 0)
        lines = raw.splitlines()
        if entry.get("skip_header"):
            lines = lines[1:]
        for line in lines:
            cells = line.split(",")
            if len(cells) > col:
                add_candidate(candidates, cells[col].strip(), "awesome_list")


# --------------------------------------------------------------------------
# metadata
# --------------------------------------------------------------------------

GQL_FIELDS = (
    "nameWithOwner stargazerCount description isArchived "
    "repositoryTopics(first: 12) { nodes { topic { name } } }"
)


def gql_batch(client: GHClient, batch: list[tuple[str, str]]) -> dict:
    """batch: list of (alias, 'owner/name'). Returns alias -> node dict|None."""
    parts = []
    for alias, full in batch:
        owner, name = full.split("/", 1)
        parts.append(
            f'{alias}: repository(owner: "{owner}", name: "{name}") {{ {GQL_FIELDS} }}'
        )
    query = "query {\n" + "\n".join(parts) + "\n}"
    payload = client.graphql(query)
    return payload.get("data") or {}


def topics_of(node: dict) -> list[str]:
    nodes = ((node.get("repositoryTopics") or {}).get("nodes")) or []
    return [n["topic"]["name"] for n in nodes if n.get("topic")]


def fetch_metadata(client, cfg, candidates) -> tuple[dict, int, int]:
    """Resolve metadata for candidates.

    Returns (resolved dict keyed by canonical full_name lc, attempted, accounted).
    `accounted` = candidates we got a definitive answer for (resolved OR 404);
    a low accounted/attempted ratio means a systemic fetch failure (rate limit),
    which the caller treats as a hard error rather than overwriting good data.
    """
    valids = [c for c in candidates.values() if valid_name(c["full_name"])]
    dropped = len(candidates) - len(valids)
    if dropped:
        log(f"  dropped {dropped} invalid candidate name(s)")

    batch_size = cfg["fetch"]["graphql_batch_size"]
    resolved: dict[str, dict] = {}
    needs_rest: list[dict] = []
    attempted = len(valids)
    accounted = 0

    for i in range(0, len(valids), batch_size):
        chunk = valids[i : i + batch_size]
        aliased = [(f"r{j}", c["full_name"]) for j, c in enumerate(chunk)]
        data = gql_batch(client, aliased)
        for (alias, _), cand in zip(aliased, chunk):
            node = data.get(alias)
            if not node:
                needs_rest.append(cand)
                continue
            accounted += 1
            _record(resolved, node["nameWithOwner"], node["stargazerCount"],
                    node.get("description"), topics_of(node),
                    bool(node.get("isArchived")), cand["sources"], None)
        log(f"  graphql batch {i // batch_size + 1}: {len(chunk)} repos")

    for cand in needs_rest:
        full = cand["full_name"]
        try:
            r = client.get_json(f"/repos/{full}", klass="core")
        except RateLimitExhausted:
            raise
        except Exception as e:  # noqa: BLE001
            log(f"  REST {full} failed: {e}")
            continue
        if r is None:  # 404 — legitimately gone; still a definitive answer
            accounted += 1
            continue
        accounted += 1
        canonical = r.get("full_name", full)
        redirected = canonical if canonical.lower() != full.lower() else None
        _record(resolved, canonical, r.get("stargazers_count"),
                r.get("description"), r.get("topics") or [],
                bool(r.get("archived")), cand["sources"], redirected)

    return resolved, attempted, accounted


def _record(resolved, full, stars, desc, topics, archived, sources, redirected):
    """Insert/merge a repo by canonical full_name (re-dedupe after rename)."""
    if stars is None:
        return
    key = full.lower()
    existing = resolved.get(key)
    if existing:
        existing["sources"].update(sources)
        if redirected and not existing.get("redirected_from"):
            existing["redirected_from"] = redirected
        return
    resolved[key] = {
        "full_name": full,
        "stars": int(stars),
        "description": clean_text(desc),
        "topics": sorted(set(topics)),
        "archived": archived,
        "sources": set(sources),
        "redirected_from": redirected,
        "readme_raw": "",
    }


# --------------------------------------------------------------------------
# raw README (network) — parsing/brief generation lives in render.py
# --------------------------------------------------------------------------

def fetch_readme_raw(client, full: str, max_chars: int) -> str:
    """Fetch the raw README markdown, capped to a leading slice that PRESERVES
    newlines (render's excerpt parser needs line structure). No markdown parsing
    here — render does that offline."""
    try:
        raw = client.get_raw(f"/repos/{full}/readme", klass="core")
    except RateLimitExhausted:
        raise
    except Exception:  # noqa: BLE001
        return ""
    if not raw:
        return ""
    return clean_multiline(raw[:max_chars])


# --------------------------------------------------------------------------
# serialize
# --------------------------------------------------------------------------

def build_payload(repos: list[dict], generated_at: str, target: int, partial: bool) -> dict:
    return {
        "generated_at": generated_at,
        "generator": "helpers/fetch.py",
        "schema_version": 2,
        "count": len(repos),
        "target_count": target,
        "partial": partial,
        "repos": [
            {
                "rank": rank,
                "full_name": r["full_name"],
                "url": f"https://github.com/{r['full_name']}",
                "stars": r["stars"],
                "description": r["description"],
                "topics": r["topics"],
                "archived": r["archived"],
                "sources": sorted(r["sources"]),
                "redirected_from": r.get("redirected_from"),
                "readme_raw": r.get("readme_raw", ""),
            }
            for rank, r in enumerate(repos, 1)
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
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--limit", type=int, default=None, help="cap output; skips floor check")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-readme", action="store_true", help="skip raw-README fetch")
    args = ap.parse_args()

    cfg = json.loads(args.config.read_text())
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    client = GHClient(get_token(), cfg["http"]["user_agent"])
    alias_lc = {k.lower(): v for k, v in cfg.get("alias_map", {}).items()}
    deny_lc = {deny_id(d).lower() for d in cfg.get("denylist", [])}

    # 1) discovery
    log("discovering candidates...")
    candidates: dict[str, dict] = {}
    discover_repo_search(client, cfg, candidates)
    discover_code_search(cfg, candidates)
    discover_awesome_lists(client, cfg, candidates)

    # 2) normalize: alias -> canonical, drop denylist, merge sources
    normalized: dict[str, dict] = {}
    for rec in candidates.values():
        cf = canon(alias_lc, rec["full_name"])
        if cf.lower() in deny_lc:
            continue
        key = cf.lower()
        n = normalized.get(key)
        if n:
            n["sources"].update(rec["sources"])
        else:
            normalized[key] = {"full_name": cf, "sources": set(rec["sources"])}
    log(f"  {len(normalized)} unique candidates after alias/deny")

    # 3) metadata
    log("fetching metadata...")
    resolved, attempted, accounted = fetch_metadata(client, cfg, normalized)
    coverage = accounted / attempted if attempted else 1.0
    log(f"  resolved {len(resolved)} repos; coverage {coverage:.2%}")

    # 4) filter (min_stars + scope + not archived) + rank + trim
    min_stars = cfg["min_stars"]
    scope = cfg.get("scope_filter")
    ranked = [
        r for r in resolved.values()
        if r["stars"] >= min_stars and in_scope(r, scope) and not r["archived"]
    ]
    log(f"  {len(ranked)} repos in scope (of {len(resolved)} resolved)")
    # Visibility: popular repos that cleared min_stars + not-archived but were
    # dropped *solely* by scope_filter. A silent scope drop is how a high-star
    # ecosystem repo (e.g. one whose name/description/topics carry no ecosystem
    # keyword) can stay invisible. Surface the worst offenders here so coverage
    # gaps show up in the log instead of hiding -- the fix is to tune
    # scope_filter / discovery in config.json, never a hand-maintained allowlist.
    scope_dropped = sorted(
        (r for r in resolved.values()
         if r["stars"] >= min_stars and not r["archived"] and not in_scope(r, scope)),
        key=lambda r: (-r["stars"], r["full_name"].lower()),
    )
    if scope_dropped:
        log(f"  note: {len(scope_dropped)} repo(s) >= {min_stars} stars dropped by "
            f"scope_filter; top: "
            + ", ".join(f"{r['full_name']}({r['stars']})" for r in scope_dropped[:10]))
    ranked.sort(key=lambda r: (-r["stars"], r["full_name"].lower()))
    target = cfg["target_count"]
    limit = args.limit or target
    top = ranked[:limit]

    # 5) floor check (skip for explicit --limit test runs)
    if args.limit is None:
        floor = cfg["fetch"]["floor_fraction"]
        if coverage < floor:
            log(f"FATAL: metadata coverage {coverage:.2%} < floor {floor:.0%}; "
                f"refusing to overwrite {args.out.name}.")
            return 1
        if not top:
            log("FATAL: zero repos resolved.")
            return 1

    # 6) raw README for final-set repos with no description (render builds the brief)
    if not args.no_readme and cfg["brief"].get("fetch_readme_when_description_empty"):
        cap = cfg["brief"]["readme_raw_max_chars"]
        n = 0
        for r in top:
            if not r["description"]:
                r["readme_raw"] = fetch_readme_raw(client, r["full_name"], cap)
                if r["readme_raw"]:
                    n += 1
        log(f"  fetched {n} raw README(s) for empty-description repos")

    partial = len(top) < target and args.limit is None
    payload = build_payload(top, generated_at, target, partial)

    if args.dry_run:
        sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        log(f"(dry-run) {len(top)} repos; not written")
        return 0

    write_atomic(payload, args.out)
    log(f"wrote {args.out} ({len(top)} repos, partial={partial})")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RateLimitExhausted as e:
        log(f"FATAL: rate limit exhausted ({e}); not writing.")
        sys.exit(2)
