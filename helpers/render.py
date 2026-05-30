#!/usr/bin/env python3
"""render.py — select the published set and regenerate README.md. Zero network.

render owns all the pure-CPU steps. It reads the full universe fetch.py produced
(helpers/repos.json) plus the two exclusion sets — helpers/out_of_scope.json (auto:
not AI/Claude) and helpers/filtered.json (manual: in-scope but redundant) — then:
  1. selects the top `render.render_count` repos by stars that are neither excluded
     nor archived, and writes that set to helpers/repos_to_render.json;
  2. categorizes, builds the short briefs (from description / topics), and lays out
     the README — a Table of Contents, a Top-N leaderboard, the long tail split into
     one table per predefined category (with a trailing "Other"), and static prose.

Usage:
  python helpers/render.py                # write ../README.md + repos_to_render.json
  python helpers/render.py --dry-run      # print to stdout, write nothing
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_IN = HERE / "repos.json"
DEFAULT_OUT_OF_SCOPE = HERE / "out_of_scope.json"
DEFAULT_FILTERED = HERE / "filtered.json"
DEFAULT_RENDER = HERE / "repos_to_render.json"
DEFAULT_CONFIG = HERE / "config.json"
DEFAULT_OUT = HERE.parent / "README.md"

MIN_REPOS = 10  # floor: refuse to overwrite README with an implausibly short render set

_TAG = re.compile(r"<[^>]*>")
_WS = re.compile(r"\s+")
_ILLEGAL = re.compile("[^\x09\x0a\x0d\x20-\U0010ffff]")
_SLUG_STRIP = re.compile(r"[^a-z0-9 _-]")


def log(msg: str) -> None:
    sys.stderr.write(msg + "\n")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def human_stars(n: int) -> str:
    if n >= 1_000_000:
        return f"⭐ {n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"⭐ {n / 1000:.1f}k"
    return f"⭐ {n}"


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


def build_brief(description: str, topics: list[str], briefcfg: dict) -> str:
    if description:
        return truncate(description, briefcfg["max_chars"])
    if topics:
        return ", ".join(topics[:5])
    return ""


# --------------------------------------------------------------------------
# parse + group
# --------------------------------------------------------------------------

def parse_repos(payload: dict, cfg: dict) -> list[dict]:
    """Build display rows (rank/url/category/brief) from a stars-sorted render set."""
    rules = cfg["category_rules"]
    briefcfg = cfg["brief"]
    repos = []
    for rank, r in enumerate(payload.get("repos", []), 1):
        full = r.get("full_name", "")
        desc_raw = r.get("description", "") or ""
        topics = r.get("topics") or []
        repos.append({
            "rank": rank,
            "full_name": full,
            "url": f"https://github.com/{full}",
            "stars": int(r.get("stars", 0)),
            "category": categorize(full, desc_raw, topics, rules),
            "description": build_brief(desc_raw, topics, briefcfg),
        })
    return repos


# --------------------------------------------------------------------------
# selection: universe - out_of_scope - filtered - archived -> top render_count
# --------------------------------------------------------------------------

def _load_ids(path: Path, key: str) -> set[str]:
    """Lowercased repo_id set from an exclusion file (out_of_scope / filtered).
    Tolerates a bare-string entry and a missing file (returns empty set)."""
    if not path.exists():
        return set()
    data = json.loads(path.read_text())
    entries = data.get(key, []) if isinstance(data, dict) else data
    out = set()
    for e in entries:
        rid = e["repo_id"] if isinstance(e, dict) else e
        if rid:
            out.add(rid.lower())
    return out


def select_render_set(universe: list[dict], excluded: set[str], render_count: int) -> list[dict]:
    """Top `render_count` repos by stars that are neither excluded nor archived.
    `universe` is already stars-sorted by fetch.py; we preserve that order."""
    eligible = [
        r for r in universe
        if not r.get("archived") and r.get("full_name", "").lower() not in excluded
    ]
    return eligible[:render_count]


def build_render_payload(repos: list[dict], generated_at: str) -> dict:
    return {
        "generated_at": generated_at,
        "generator": "helpers/render.py",
        "count": len(repos),
        "repos": [
            {
                "full_name": r["full_name"],
                "stars": int(r.get("stars", 0)),
                "description": r.get("description", "") or "",
                "topics": r.get("topics") or [],
            }
            for r in repos
        ],
    }


def write_atomic(payload_or_text, out: Path) -> None:
    tmp = out.with_suffix(out.suffix + ".tmp")
    if isinstance(payload_or_text, str):
        tmp.write_text(payload_or_text, encoding="utf-8")
    else:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload_or_text, f, indent=2, ensure_ascii=False)
            f.write("\n")
    tmp.replace(out)


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
**not hand-curated**: `fetch.py` does an exhaustive sweep of **every** public repo
at or above the star floor (`helpers/repos.json`), so discovery is complete by
construction. `render.py` then removes the two exclusion sets and publishes the top
results by **live star count**.

- **In scope:** Claude Code skills, agents, plugins, harnesses, memory and
  orchestration; the MCP tool-protocol layer; general-purpose and provider-neutral
  LLM/agent frameworks; and multi-model coding-agent CLIs that support Claude
  (opencode, OpenHands, goose).
- **Out of scope (`helpers/out_of_scope.json`, auto):** repos whose
  name/description/topics don't match any ecosystem term — classified out
  automatically by `scope_filter`, regenerated every run.
- **Filtered (`helpers/filtered.json`, editorial):** repos that *are* AI/Claude-
  adjacent but excluded as redundant — single-vendor / non-Claude competing CLIs
  (e.g. gemini-cli, codex), API gateways/proxies, generic chat UIs, and
  leaked/rights-infringing content.
- **Floor:** repositories under 20,000 stars, and archived repositories, are excluded.
- **Star counts** are a point-in-time snapshot and drift daily.
"""

CONTRIBUTING = """## Contributing

The published tables are generated — don't edit them by hand. To change what
appears: adjust `scope_filter` / `category_rules` in `helpers/config.json` (to
re-classify what counts as in-ecosystem), or add a `{ "repo_id": "owner/name",
"reason": "..." }` entry to `helpers/filtered.json` (to drop an AI-adjacent but
redundant repo). There is **no allowlist** and no hand-edited ranking — the daily
workflow re-sweeps and re-renders.
"""

DISCLAIMER = """## Disclaimer

This is an automatically generated index of **publicly available** GitHub
repository metadata (names, owners, star counts, and the projects' own
descriptions). It does **not** host, mirror, or redistribute any third-party
source code or content.

- **No endorsement.** Inclusion is purely algorithmic (by public star count) and
  is not a recommendation, endorsement, or vetting of any listed project. We do
  not audit listed repositories for security, licensing, or legal compliance.
- **Third-party links.** Links point to independent repositories we do not
  control and are not responsible for. Descriptions are the projects' own text,
  reproduced as factual metadata; they are not statements by this project.
- **Trademarks.** "Claude", "Anthropic", and all other product and company names
  are trademarks of their respective owners. References are nominative (for
  identification only) and imply no affiliation or sponsorship.
- **Removal.** To request removal of an entry, open an issue; off-scope or
  rights-infringing repositories can be excluded via `helpers/filtered.json`.
"""

LICENSE = """## License

The contents of **this** repository (scripts and generated index) are released
under [CC0-1.0](https://creativecommons.org/publicdomain/zero/1.0/) — public
domain. Listed third-party repositories remain under their own licenses, held by
their respective owners.
"""


def build_readme(payload: dict, cfg: dict) -> str:
    repos = parse_repos(payload, cfg)
    top_n = cfg.get("top_table_size", 30)
    generated_at = fmt_date(payload.get("generated_at", ""))

    top = repos[:top_n]
    tail = repos[top_n:]
    grouped = group_tail(tail, cfg) if tail else []

    out = [
        "# Awesome Claude Code & Agent Tools",
        "",
        "> The most-starred repositories in the Claude Code / skills / agents / MCP ecosystem.",
        f"> **Updated at {generated_at}** (last successful refresh). "
        f"{len(repos)} repositories, sorted by live GitHub stars, descending.",
    ]

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
        f"- [Disclaimer](#{slugify('Disclaimer')})",
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

    out += [SCOPE, CONTRIBUTING, DISCLAIMER, LICENSE]
    return "\n".join(out).rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", type=Path, default=DEFAULT_IN)
    ap.add_argument("--out-of-scope", type=Path, default=DEFAULT_OUT_OF_SCOPE)
    ap.add_argument("--filtered", type=Path, default=DEFAULT_FILTERED)
    ap.add_argument("--render-out", type=Path, default=DEFAULT_RENDER)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = json.loads(args.config.read_text())
    universe_doc = json.loads(args.infile.read_text())
    universe = universe_doc.get("repos", [])

    # universe - out_of_scope (auto) - filtered (manual) - archived, top N by stars
    oos_ids = _load_ids(args.out_of_scope, "out_of_scope")
    filtered_ids = _load_ids(args.filtered, "filtered")
    excluded = oos_ids | filtered_ids
    render_count = cfg["render"]["render_count"]
    render_set = select_render_set(universe, excluded, render_count)
    log(f"universe {len(universe)} | excluded {len(excluded)} | "
        f"render set {len(render_set)} (cap {render_count})")

    # surface dead filtered.json entries (renamed/dropped-below-floor repos) — they
    # match nothing in the universe, so they silently do nothing until audited.
    universe_ids = {r.get("full_name", "").lower() for r in universe}
    stale = sorted(filtered_ids - universe_ids)
    if stale:
        noun = "entry matches" if len(stale) == 1 else "entries match"
        log(f"WARNING: {len(stale)} filtered.json {noun} no repo in the universe "
            f"(renamed or below the star floor?): {', '.join(stale)}")

    # floor guards the PUBLISHED set, not the universe
    if len(render_set) < MIN_REPOS:
        log(f"FATAL: only {len(render_set)} repos in the render set (< {MIN_REPOS}); "
            f"refusing to overwrite README.")
        return 1

    generated_at = universe_doc.get("generated_at") or datetime.now(
        timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    render_payload = build_render_payload(render_set, generated_at)
    readme = build_readme(render_payload, cfg)

    if args.dry_run:
        sys.stdout.write(readme)
        log(f"(dry-run) render set {len(render_set)} repos; nothing written")
        return 0

    write_atomic(render_payload, args.render_out)
    write_atomic(readme, args.out)
    log(f"wrote {args.out} + {args.render_out} ({len(render_set)} repos)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
