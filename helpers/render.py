#!/usr/bin/env python3
"""render.py — regenerate README.md from helpers/repos.json. Zero network.

render owns all the pure-CPU steps: it reads the raw snapshot fetch.py produced
(metadata + raw README text), then categorizes, builds the short briefs, and lays
out the README — a Table of Contents, a Top-N leaderboard, the long tail split
into one table per predefined category (with a trailing "Other" catch-all), and
static scope/contributing/license prose.

Usage:
  python helpers/render.py                # write ../README.md
  python helpers/render.py --dry-run      # print to stdout
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_IN = HERE / "repos.json"
DEFAULT_CONFIG = HERE / "config.json"
DEFAULT_OUT = HERE.parent / "README.md"

MIN_REPOS = 10  # floor: refuse to overwrite README with an implausibly short list

_TAG = re.compile(r"<[^>]*>")
_WS = re.compile(r"\s+")
_ILLEGAL = re.compile("[^\x09\x0a\x0d\x20-\U0010ffff]")
_SLUG_STRIP = re.compile(r"[^a-z0-9 _-]")

# README-excerpt parsing (ported from fetch.py; operates on saved raw text, no network)
_SKIP_LINE = re.compile(r"^\s*(#|>|<|\[!\[|!\[|---|===|\|)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_INLINE = re.compile(r"[`*_]+")


def log(msg: str) -> None:
    sys.stderr.write(msg + "\n")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def human_stars(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def clean_text(s: str) -> str:
    if not s:
        return ""
    s = _ILLEGAL.sub("", s)
    return _WS.sub(" ", s).strip()


def truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0].rstrip()
    return (cut or s[:n].rstrip()) + "…"


def cell(s: str) -> str:
    """Sanitize free text for a markdown table cell."""
    if not s:
        return "—"
    s = _TAG.sub("", s)              # drop angle-bracket HTML tags
    s = _WS.sub(" ", s).strip()
    s = s.lstrip("#>-*").strip()     # neutralize leading markdown control chars
    s = s.replace("|", "\\|")
    return s or "—"


def fmt_date(iso: str) -> str:
    # "2026-05-30T06:30:00Z" -> "2026-05-30 06:30 UTC"
    m = re.match(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})", iso or "")
    return f"{m.group(1)} {m.group(2)} UTC" if m else (iso or "unknown")


def slugify(heading: str) -> str:
    """GitHub heading-anchor slug: lowercase, drop chars outside [a-z0-9 _-],
    spaces -> '-', no hyphen collapsing. 'MCP server / tooling' -> 'mcp-server--tooling'."""
    s = heading.strip().lower()
    s = _SLUG_STRIP.sub("", s)
    return s.replace(" ", "-")


# --------------------------------------------------------------------------
# categorize + brief (pure CPU; were in fetch.py before the split)
# --------------------------------------------------------------------------

def categorize(full_name: str, description: str, topics: list[str], rules: dict) -> str:
    owner = full_name.split("/", 1)[0].lower()
    tps = [t.lower() for t in topics]
    text = " ".join([full_name.lower(), description.lower(), *tps])

    for kr in rules["keyword_rules"]:
        if "match_owner" in kr and owner in [o.lower() for o in kr["match_owner"]]:
            return kr["category"]
    topic_map = {k.lower(): v for k, v in rules["topic_map"].items()}
    for t in tps:
        if t in topic_map:
            return topic_map[t]
    for kr in rules["keyword_rules"]:
        for kw in kr.get("any", []):
            if kw.lower() in text:
                return kr["category"]
    return rules["default_category"]


def readme_excerpt(raw: str) -> str:
    """First prose paragraph of a raw README, stripped of markdown. No network."""
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


def build_brief(description: str, topics: list[str], readme_raw: str, briefcfg: dict) -> str:
    if description:
        return truncate(description, briefcfg["max_chars"])
    if readme_raw:
        para = readme_excerpt(readme_raw)
        if para:
            return truncate(para, briefcfg["readme_excerpt_max_chars"])
    if topics:
        return ", ".join(topics[:5])
    return ""


# --------------------------------------------------------------------------
# parse + group
# --------------------------------------------------------------------------

def parse_repos(payload: dict, cfg: dict) -> list[dict]:
    rules = cfg["category_rules"]
    briefcfg = cfg["brief"]
    repos = []
    for r in payload.get("repos", []):
        full = r.get("full_name", "")
        desc_raw = r.get("description", "") or ""
        topics = r.get("topics") or []
        readme_raw = r.get("readme_raw", "") or ""
        repos.append({
            "rank": int(r.get("rank", 0)),
            "full_name": full,
            "url": r.get("url", "") or f"https://github.com/{full}",
            "stars": int(r.get("stars", 0)),
            "category": categorize(full, desc_raw, topics, rules),
            "description": build_brief(desc_raw, topics, readme_raw, briefcfg),
        })
    repos.sort(key=lambda r: r["rank"])
    return repos


def group_tail(tail: list[dict], cfg: dict) -> list[tuple[str, list[dict]]]:
    """Group rank>N repos using the predefined static `category_order`.

    Categories render in config order (empty ones skipped); a trailing "Other"
    bucket collects the default category and anything not listed. No repo is ever
    dropped. Warns (non-fatal) if "Other" grows large or if a rule can emit a
    category missing from `category_order`.
    """
    rcfg = cfg.get("render", {})
    order = rcfg.get("category_order", [])
    other_label = rcfg.get("other_category_label", "Other")
    other_max = rcfg.get("other_max", 10)
    rules = cfg["category_rules"]
    default_cat = rules["default_category"]

    # sync guard: every category a rule can emit should be listed (or it's the default)
    emitted = set(rules["topic_map"].values())
    emitted.update(kr["category"] for kr in rules["keyword_rules"])
    order_set = set(order)
    missing = [c for c in sorted(emitted) if c not in order_set and c != default_cat]
    if missing:
        log(f"WARNING: categories emitted by rules but absent from render.category_order "
            f"(they will fall into '{other_label}'): {missing}")

    groups: dict[str, list[dict]] = {}
    for r in tail:
        groups.setdefault(r["category"], []).append(r)

    result = []
    for cat in order:
        items = groups.get(cat)
        if items:
            result.append((cat, sorted(items, key=lambda r: r["rank"])))

    other = [r for cat, items in groups.items() if cat not in order_set for r in items]
    if other:
        other.sort(key=lambda r: r["rank"])
        if len(other) >= other_max:
            names = ", ".join(r["full_name"] for r in other)
            log(f"WARNING: '{other_label}' has {len(other)} repos (>= {other_max}); "
                f"consider adding a category rule. Repos: {names}")
        result.append((other_label, other))
    return result


# --------------------------------------------------------------------------
# tables
# --------------------------------------------------------------------------

def repo_link(r: dict) -> str:
    return f"[{r['full_name']}]({r['url']})"


def top_table(repos: list[dict]) -> str:
    lines = ["| # | Repo | Stars | Category | Description |",
             "|---|------|------:|----------|-------------|"]
    for r in repos:
        lines.append(
            f"| {r['rank']} | {repo_link(r)} | {human_stars(r['stars'])} "
            f"| {cell(r['category'])} | {cell(r['description'])} |"
        )
    return "\n".join(lines)


def category_table(repos: list[dict]) -> str:
    lines = ["| Repo | Stars | Description |",
             "|------|------:|-------------|"]
    for r in repos:
        lines.append(
            f"| {repo_link(r)} | {human_stars(r['stars'])} | {cell(r['description'])} |"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# static prose
# --------------------------------------------------------------------------

SCOPE = """## Scope & methodology

This list is generated automatically, once per day, by `helpers/fetch.py` →
`helpers/render.py` (see `.github/workflows/refresh-ranking.yml`). Membership is
**not hand-curated**: candidates are discovered from three signals — GitHub
repository search (keyword/topic), structural code search (`SKILL.md` /
`CLAUDE.md`), and curated awesome-list aggregation — then deduped, re-ranked by
**live star count**, and trimmed to the top results.

- **In scope:** Claude Code skills, agents, plugins, harnesses, memory and
  orchestration; the MCP tool-protocol layer; general-purpose and provider-neutral
  LLM/agent frameworks; and multi-model coding-agent CLIs that support Claude
  (opencode, OpenHands, goose).
- **Out of scope:** single-vendor / non-Claude-compatible competing coding-agent
  CLIs (e.g. gemini-cli, codex), and general chat UIs.
- **Floor:** repositories under 20,000 stars, and archived repositories, are excluded.
- **Star counts** are a point-in-time snapshot and drift daily.
"""

CONTRIBUTING = """## Contributing

The published tables are generated — don't edit them by hand. To change what
appears, edit `helpers/config.json`: discovery queries, the `denylist` (to exclude
a competing single-vendor CLI), `alias_map`, `scope_filter`, and the category
rules. The daily workflow re-fetches and re-ranks.
"""

LICENSE = """## License

[CC0-1.0](https://creativecommons.org/publicdomain/zero/1.0/) — public domain.
"""


def build_readme(payload: dict, cfg: dict) -> str:
    repos = parse_repos(payload, cfg)
    top_n = cfg.get("top_table_size", 30)
    generated_at = fmt_date(payload.get("generated_at", ""))
    partial = bool(payload.get("partial"))

    top = repos[:top_n]
    tail = repos[top_n:]
    grouped = group_tail(tail, cfg) if tail else []

    out = [
        "# Awesome Claude Code & Agent Tools",
        "",
        "> The most-starred repositories in the Claude Code / skills / agents / MCP ecosystem.",
        f"> **Updated at {generated_at}** (last successful refresh). Sorted by live GitHub stars, descending.",
    ]
    if partial:
        out.append(">")
        out.append(f"> ⚠️ Partial refresh — {len(repos)} repos (fewer than the target).")

    # table of contents
    top_heading = f"Top {len(top)}"
    out += ["", "## Contents", "", f"- [{top_heading}](#{slugify(top_heading)})"]
    if grouped:
        out.append(f"- [By category](#{slugify('By category')})")
        for heading, _ in grouped:
            out.append(f"  - [{heading}](#{slugify(heading)})")
    out += [
        f"- [Scope & methodology](#{slugify('Scope & methodology')})",
        f"- [Contributing](#{slugify('Contributing')})",
        f"- [License](#{slugify('License')})",
    ]

    out += ["", f"## {top_heading}", "", top_table(top), ""]

    if grouped:
        out += [
            "## By category",
            "",
            "_The long tail below the top leaderboard, grouped by category "
            "(top-ranked repos above are not repeated here)._",
            "",
        ]
        for heading, items in grouped:
            out += [f"### {heading}", "", category_table(items), ""]

    out += [SCOPE, CONTRIBUTING, LICENSE]
    return "\n".join(out).rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", type=Path, default=DEFAULT_IN)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    payload = json.loads(args.infile.read_text())
    count = len(payload.get("repos", []))
    if count < MIN_REPOS:
        log(f"FATAL: only {count} repos in {args.infile.name} (< {MIN_REPOS}); "
            f"refusing to overwrite README.")
        return 1

    cfg = json.loads(args.config.read_text())
    readme = build_readme(payload, cfg)

    if args.dry_run:
        sys.stdout.write(readme)
        return 0

    tmp = args.out.with_suffix(args.out.suffix + ".tmp")
    tmp.write_text(readme, encoding="utf-8")
    tmp.replace(args.out)
    log(f"wrote {args.out} ({count} repos)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
