#!/usr/bin/env python3
"""render.py — regenerate README.md from helpers/repos.xml. Zero network.

Layout: a Top-N leaderboard table, then the long tail split into one table per
category (small categories folded into a trailing "Other" table), plus an
explicit "Updated at" note and static scope/contributing/license prose.

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
from xml.etree import ElementTree as ET

HERE = Path(__file__).resolve().parent
DEFAULT_XML = HERE / "repos.xml"
DEFAULT_CONFIG = HERE / "config.json"
DEFAULT_OUT = HERE.parent / "README.md"

MIN_REPOS = 10  # floor: refuse to overwrite README with an implausibly short list

_TAG = re.compile(r"<[^>]*>")
_WS = re.compile(r"\s+")


def log(msg: str) -> None:
    sys.stderr.write(msg + "\n")


def human_stars(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


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


def parse_repos(root: ET.Element) -> list[dict]:
    repos = []
    for el in root.findall("repo"):
        repos.append({
            "rank": int(el.get("rank", "0")),
            "full_name": el.findtext("full_name", ""),
            "url": el.findtext("url", ""),
            "stars": int(el.findtext("stars", "0")),
            "category": el.findtext("category", "") or "Uncategorized",
            "description": el.findtext("description", "") or "",
            "archived": el.findtext("archived", "false") == "true",
        })
    repos.sort(key=lambda r: r["rank"])
    return repos


def repo_link(r: dict) -> str:
    link = f"[{r['full_name']}]({r['url']})"
    return link + " *(archived)*" if r["archived"] else link


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


def group_tail(tail: list[dict], cfg: dict) -> list[tuple[str, list[dict]]]:
    """Group rank>N repos by category; fold small categories into 'Other'.

    Returns ordered list of (heading, repos): big categories by total stars desc,
    then default category, then the folded 'Other' bucket — both always last.
    """
    min_rows = cfg.get("render", {}).get("min_rows_per_category", 3)
    other_label = cfg.get("render", {}).get("other_category_label", "Other")
    default_cat = cfg.get("category_rules", {}).get("default_category")

    groups: dict[str, list[dict]] = {}
    for r in tail:
        groups.setdefault(r["category"], []).append(r)

    big, other = {}, []
    for cat, items in groups.items():
        if len(items) >= min_rows:
            big[cat] = items
        else:
            other.extend(items)

    def total(cat: str) -> int:
        return sum(r["stars"] for r in big[cat])

    ordered = sorted(big, key=lambda c: (-total(c), c.lower()))
    # default category always sinks below the other big categories
    if default_cat in ordered:
        ordered = [c for c in ordered if c != default_cat] + [default_cat]

    result = [(cat, sorted(big[cat], key=lambda r: r["rank"])) for cat in ordered]
    if other:
        result.append((other_label, sorted(other, key=lambda r: r["rank"])))
    return result


SCOPE = """## Scope & methodology

This list is generated automatically, once per day, by `helpers/fetch.py` →
`helpers/render.py` (see `.github/workflows/refresh-ranking.yml`). Membership is
**not hand-curated**: candidates are discovered from three signals — GitHub
repository search (keyword/topic), structural code search (`SKILL.md` /
`CLAUDE.md`), and curated awesome-list aggregation — then deduped, re-ranked by
**live star count**, and trimmed to the top results.

- **In scope:** Claude Code skills, agents, plugins, harnesses, memory and
  orchestration; the MCP tool-protocol layer; and directly adjacent coding-agent
  CLIs (opencode, codex, gemini-cli, OpenHands, goose).
- **Out of scope:** general-purpose AI frameworks that aren't Claude/skill/agent
  specific, and general chat UIs.
- **Star counts** are a point-in-time snapshot and drift daily.
"""

CONTRIBUTING = """## Contributing

The published tables are generated — don't edit them by hand. To change what
appears, edit `helpers/config.json` (discovery queries, `allowlist`/`denylist`,
`alias_map`, category rules). The daily workflow re-fetches and re-ranks.
"""

LICENSE = """## License

[CC0-1.0](https://creativecommons.org/publicdomain/zero/1.0/) — public domain.
"""


def build_readme(root: ET.Element, cfg: dict) -> str:
    repos = parse_repos(root)
    top_n = cfg.get("top_table_size", 30)
    generated_at = fmt_date(root.get("generated_at", ""))
    partial = root.get("partial") == "true"

    top = repos[:top_n]
    tail = repos[top_n:]

    out = [
        "# Awesome Claude Code & Agent Tools",
        "",
        "> The most-starred repositories in the Claude Code / skills / agents / MCP ecosystem.",
        f"> **Updated at {generated_at}** (last successful refresh). Sorted by live GitHub stars, descending.",
    ]
    if partial:
        out.append(">")
        out.append(f"> ⚠️ Partial refresh — {len(repos)} repos (fewer than the target).")
    out += ["", f"## Top {len(top)}", "", top_table(top), ""]

    if tail:
        out += [
            "## By category",
            "",
            "_The long tail below the top leaderboard, grouped by category "
            "(top-ranked repos above are not repeated here)._",
            "",
        ]
        for heading, items in group_tail(tail, cfg):
            out += [f"### {heading}", "", category_table(items), ""]

    out += [SCOPE, CONTRIBUTING, LICENSE]
    return "\n".join(out).rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--xml", type=Path, default=DEFAULT_XML)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = ET.parse(args.xml).getroot()
    count = len(root.findall("repo"))
    if count < MIN_REPOS:
        log(f"FATAL: only {count} repos in {args.xml.name} (< {MIN_REPOS}); "
            f"refusing to overwrite README.")
        return 1

    cfg = json.loads(args.config.read_text())
    readme = build_readme(root, cfg)

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
