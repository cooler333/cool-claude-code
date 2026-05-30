#!/usr/bin/env python3
"""fetch.py — discover, rank, and serialize the top Claude-Code/AI repos to XML.

Pipeline: discover candidates (repo search + `gh` code search + awesome-list)
-> normalize/dedupe/alias/allow/deny -> validate -> authoritative metadata via
batched GraphQL -> REST fallback for renames -> re-dedupe -> rank+trim ->
heuristic categorize + brief -> deterministic atomic XML write.

stdlib only. Network goes through helpers/ghclient.py. Exits non-zero on hard
failure so CI never renders/commits degraded data.

Usage:
  python helpers/fetch.py                 # full run -> helpers/repos.xml
  python helpers/fetch.py --dry-run       # print XML to stdout, don't write
  python helpers/fetch.py --limit 20      # small run (skips floor check)
  python helpers/fetch.py --no-readme     # skip README-excerpt brief fallback
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree as ET

from ghclient import GHClient, RateLimitExhausted, get_token

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"
DEFAULT_XML = HERE / "repos.xml"

OWNER_RE = re.compile(r"^[A-Za-z0-9-]+$")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# Strip characters illegal in XML 1.0 text (keep tab/newline/CR + valid ranges).
_ILLEGAL_XML = re.compile(
    "[^\x09\x0a\x0d\x20-퟿-�\U00010000-\U0010ffff]"
)
_WS = re.compile(r"\s+")


def log(msg: str) -> None:
    sys.stderr.write(msg + "\n")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def clean_text(s: str | None) -> str:
    if not s:
        return ""
    s = _ILLEGAL_XML.sub("", s)
    return _WS.sub(" ", s).strip()


def truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0].rstrip()
    return (cut or s[:n].rstrip()) + "…"


def valid_name(full: str) -> bool:
    parts = full.split("/")
    if len(parts) != 2:
        return False
    owner, name = parts
    return bool(OWNER_RE.match(owner) and NAME_RE.match(name))


def canon(alias_map_lc: dict[str, str], full: str) -> str:
    """Apply alias map (case-insensitive); return canonical owner/name."""
    return alias_map_lc.get(full.lower(), full)


def in_scope(repo: dict, scope: dict) -> bool:
    """Relevance gate: loose search pulls in unrelated giants (awesome lists,
    roadmaps). Keep only repos whose name/description/topics match an ecosystem
    term. Allowlisted repos bypass this upstream."""
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


def discover_code_search(cfg: dict, candidates: dict) -> None:
    """Code search via `gh` (newer code-search API; raw REST 422s on bare qualifiers)."""
    for fn in cfg["discovery"].get("code_search_filenames", []):
        try:
            out = subprocess.run(
                ["gh", "search", "code", "--filename", fn,
                 "--json", "repository", "--limit", "100"],
                capture_output=True, text=True, timeout=120,
            )
        except (FileNotFoundError, subprocess.SubprocessError) as e:
            log(f"  code_search '{fn}' skipped (gh unavailable: {e})")
            return
        if out.returncode != 0:
            log(f"  code_search '{fn}' skipped (gh rc={out.returncode}): "
                f"{out.stderr.strip()[:200]}")
            continue
        try:
            rows = json.loads(out.stdout or "[]")
        except json.JSONDecodeError:
            continue
        for row in rows:
            repo = row.get("repository") or {}
            name = repo.get("nameWithOwner") or repo.get("fullName") or ""
            add_candidate(candidates, name, "code_search")
        time.sleep(6.5)  # respect code-search ~10/min


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
    }


# --------------------------------------------------------------------------
# categorize + brief
# --------------------------------------------------------------------------

def categorize(repo: dict, rules: dict) -> tuple[str, str]:
    owner = repo["full_name"].split("/", 1)[0].lower()
    topics = [t.lower() for t in repo["topics"]]
    text = " ".join([repo["full_name"].lower(), repo["description"].lower(), *topics])

    for kr in rules["keyword_rules"]:
        if "match_owner" in kr and owner in [o.lower() for o in kr["match_owner"]]:
            return kr["category"], "keyword_rules"
    topic_map = {k.lower(): v for k, v in rules["topic_map"].items()}
    for t in topics:
        if t in topic_map:
            return topic_map[t], "topic_map"
    for kr in rules["keyword_rules"]:
        for kw in kr.get("any", []):
            if kw.lower() in text:
                return kr["category"], "keyword_rules"
    return rules["default_category"], "default"


def build_brief(client, repo, briefcfg, fetch_readme) -> tuple[str, str]:
    desc = repo["description"]
    if desc:
        return truncate(desc, briefcfg["max_chars"]), "description"
    if fetch_readme and briefcfg.get("fetch_readme_when_description_empty"):
        para = readme_excerpt(client, repo["full_name"])
        if para:
            return truncate(para, briefcfg["readme_excerpt_max_chars"]), "readme_excerpt"
    if repo["topics"]:
        return ", ".join(repo["topics"][:5]), "topics"
    return "", "topics"


_SKIP_LINE = re.compile(r"^\s*(#|>|<|\[!\[|!\[|---|===|\|)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_INLINE = re.compile(r"[`*_]+")


def readme_excerpt(client, full: str) -> str:
    try:
        raw = client.get_raw(f"/repos/{full}/readme", klass="core")
    except RateLimitExhausted:
        raise
    except Exception:  # noqa: BLE001
        return ""
    if not raw:
        return ""
    buf = []
    for line in raw.splitlines():
        if not line.strip():
            if buf:
                break
            continue
        if _SKIP_LINE.match(line):
            continue
        buf.append(line.strip())
    text = " ".join(buf)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_INLINE.sub("", text)
    return clean_text(text)


# --------------------------------------------------------------------------
# serialize
# --------------------------------------------------------------------------

def build_xml(repos: list[dict], generated_at: str, target: int, partial: bool) -> ET.Element:
    root = ET.Element("repos")
    root.set("generated_at", generated_at)
    root.set("generator", "helpers/fetch.py")
    root.set("schema_version", "1")
    root.set("count", str(len(repos)))
    root.set("target_count", str(target))
    root.set("partial", "true" if partial else "false")
    for rank, r in enumerate(repos, 1):
        el = ET.SubElement(root, "repo")
        el.set("rank", str(rank))
        ET.SubElement(el, "full_name").text = r["full_name"]
        ET.SubElement(el, "url").text = f"https://github.com/{r['full_name']}"
        ET.SubElement(el, "stars").text = str(r["stars"])
        cat = ET.SubElement(el, "category")
        cat.set("source", r["category_source"])
        cat.text = r["category"]
        desc = ET.SubElement(el, "description")
        desc.set("source", r["brief_source"])
        desc.text = r["brief"]
        topics_el = ET.SubElement(el, "topics")
        for t in r["topics"]:
            ET.SubElement(topics_el, "topic").text = t
        disc = ET.SubElement(el, "discovery")
        for s in sorted(r["sources"]):
            ET.SubElement(disc, "source").text = s
        ET.SubElement(el, "archived").text = "true" if r["archived"] else "false"
        if r.get("redirected_from"):
            ET.SubElement(el, "redirected_from").text = r["redirected_from"]
    ET.indent(root, space="  ")
    return root


def write_atomic(root: ET.Element, out: Path) -> None:
    tmp = out.with_suffix(out.suffix + ".tmp")
    tree = ET.ElementTree(root)
    with tmp.open("wb") as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        tree.write(f, encoding="utf-8", xml_declaration=False)
        f.write(b"\n")
    tmp.replace(out)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--out", type=Path, default=DEFAULT_XML)
    ap.add_argument("--limit", type=int, default=None, help="cap output; skips floor check")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-readme", action="store_true", help="skip README-excerpt brief")
    args = ap.parse_args()

    cfg = json.loads(args.config.read_text())
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    client = GHClient(get_token(), cfg["http"]["user_agent"])
    alias_lc = {k.lower(): v for k, v in cfg.get("alias_map", {}).items()}
    deny_lc = {x.lower() for x in cfg.get("denylist", [])}
    allow_lc = {x.lower() for x in cfg.get("allowlist", [])}

    # 1) discovery
    log("discovering candidates...")
    candidates: dict[str, dict] = {}
    discover_repo_search(client, cfg, candidates)
    discover_code_search(cfg, candidates)
    discover_awesome_lists(client, cfg, candidates)
    for full in cfg.get("allowlist", []):
        add_candidate(candidates, full, "allowlist")

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

    # 4) rank + trim (allowlisted repos bypass min_stars AND the scope filter)
    min_stars = cfg["min_stars"]
    scope = cfg.get("scope_filter")
    ranked = [
        r for r in resolved.values()
        if r["full_name"].lower() in allow_lc
        or (r["stars"] >= min_stars and in_scope(r, scope))
    ]
    log(f"  {len(ranked)} repos in scope (of {len(resolved)} resolved)")
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

    # 6) categorize + brief
    log("categorizing + building briefs...")
    rules = cfg["category_rules"]
    for r in top:
        r["category"], r["category_source"] = categorize(r, rules)
        r["brief"], r["brief_source"] = build_brief(
            client, r, cfg["brief"], fetch_readme=not args.no_readme
        )

    partial = len(top) < target and args.limit is None
    root = build_xml(top, generated_at, target, partial)

    if args.dry_run:
        sys.stdout.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            + ET.tostring(root, encoding="unicode") + "\n"
        )
        log(f"(dry-run) {len(top)} repos; not written")
        return 0

    write_atomic(root, args.out)
    log(f"wrote {args.out} ({len(top)} repos, partial={partial})")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RateLimitExhausted as e:
        log(f"FATAL: rate limit exhausted ({e}); not writing.")
        sys.exit(2)
