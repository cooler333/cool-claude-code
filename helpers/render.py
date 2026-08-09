#!/usr/bin/env python3
"""render.py — select the published set and regenerate README.md. Zero network.

render owns all the pure-CPU steps. It reads the full universe fetch.py produced
(helpers/repos.json) plus the two exclusion sets — helpers/out_of_scope.json (auto:
not AI/Claude) and helpers/filtered.json (manual: in-scope but redundant) — then:
  1. selects the top `render.render_count` repos by stars that are neither excluded
     nor archived, and writes that set to helpers/repos_to_render.json;
  2. categorizes, builds the short briefs (from description / topics), and lays out
     the README — a Table of Contents, a Top-N leaderboard, a "Trending this week"
     cut, the long tail split into one table per predefined category (with a trailing
     "Other"), and static prose.

Momentum (rank movement, star growth) comes from older helpers/repos.json snapshots
in git history via helpers/trend.py — the only impure read in this half of the
pipeline, and still offline. Both baselines are optional: without them the affected
columns are simply omitted (see collect_baselines).

Usage:
  python helpers/render.py                # write ../README.md + repos_to_render.json
  python helpers/render.py --dry-run      # print to stdout, write nothing
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import trend

HERE = Path(__file__).resolve().parent
DEFAULT_IN = HERE / "repos.json"
DEFAULT_OUT_OF_SCOPE = HERE / "out_of_scope.json"
DEFAULT_FILTERED = HERE / "filtered.json"
DEFAULT_RENDER = HERE / "repos_to_render.json"
DEFAULT_CONFIG = HERE / "config.json"
DEFAULT_OUT = HERE.parent / "README.md"

MIN_REPOS = 10  # floor: refuse to overwrite README with an implausibly short render set
TRENDING_HEADING = "Trending this week"

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
    # non-breaking space ( ) keeps the star glued to the number so the
    # right-aligned Stars column never wraps "⭐" onto its own line.
    if n >= 1_000_000:
        return f"⭐ {n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"⭐ {n / 1000:.1f}k"
    return f"⭐ {n}"


def human_delta(n: int | None) -> str:
    return "—" if n is None else f"{n:+,}"


def human_pct(p: float | None) -> str:
    return "—" if p is None else f"{p:+.1f}%"


def rank_move(prev_rank: int | None, rank: int) -> str:
    """Position movement, positive = climbed. Only called when a baseline exists,
    so a missing prev_rank means the repo was not in the baseline's published set."""
    if prev_rank is None:
        return "new"
    moved = prev_rank - rank
    return "—" if moved == 0 else f"{moved:+d}"


def display_names(full_names: list[str]) -> dict[str, str]:
    """{full_name: label} for the Name column — the short name after "/".

    The owner is redundant once the name is a link, and dropping it buys a lot of
    table width. It is kept only where the published set holds namesakes (three
    `skills` today), because two rows rendering an identical label would be worse
    than a long one. Compared case-insensitively so a visual twin still disambiguates.
    """
    shorts = {fn: (fn.split("/", 1)[1] if "/" in fn else fn) for fn in full_names}
    clashing = {s for s, n in Counter(v.lower() for v in shorts.values()).items() if n > 1}
    return {
        fn: f"{short} ({fn.split('/', 1)[0]})" if short.lower() in clashing else short
        for fn, short in shorts.items()
    }


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

    def hit(kr):
        return any(kw.lower() in text for kw in kr.get("any", []))

    for kr in rules["keyword_rules"]:
        if "match_owner" in kr and owner in [o.lower() for o in kr["match_owner"]]:
            return kr["category"]
    # A rule flagged `beats_topics` is checked before topic_map: what a repo *is*
    # (a course, a tutorial) is more decisive than the topics it tags itself with,
    # and teaching material tags the subject it teaches ("mcp", "agents").
    for kr in rules["keyword_rules"]:
        if kr.get("beats_topics") and hit(kr):
            return kr["category"]
    topic_map = {k.lower(): v for k, v in rules["topic_map"].items()}
    for t in tps:
        if t in topic_map:
            return topic_map[t]
    for kr in rules["keyword_rules"]:
        if hit(kr):
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

def parse_repos(render_set: list[dict], cfg: dict) -> list[dict]:
    """Build display rows (rank/url/category/brief) from a stars-sorted render set.
    Takes the full repo dicts (from repos.json) in memory — the on-disk
    repos_to_render.json holds only repo references, not this metadata."""
    rules = cfg["category_rules"]
    briefcfg = cfg["brief"]
    names = display_names([r.get("full_name", "") for r in render_set])
    repos = []
    for rank, r in enumerate(render_set, 1):
        full = r.get("full_name", "")
        desc_raw = r.get("description", "") or ""
        topics = r.get("topics") or []
        repos.append({
            "rank": rank,
            "full_name": full,
            "name": names[full],
            "url": f"https://github.com/{full}",
            "stars": int(r.get("stars", 0)),
            "category": categorize(full, desc_raw, topics, rules),
            "description": build_brief(desc_raw, topics, briefcfg),
            # filled by annotate_momentum; None = not covered by the baseline
            "base_rank": None,
            "delta_stars": None,
            "pct_stars": None,
        })
    return repos


def annotate_momentum(repos: list[dict], base_ranks: dict | None,
                      base_stars: dict | None) -> list[str]:
    """Attach rank movement and star growth in place; return the repos the baseline
    didn't cover (new entrants — their gain is unknown, not zero).

    With no baseline at all (short or shallow history) the fields stay None and the
    whole column pair is dropped from the render. A repo missing from the baseline's
    published set is not an orphan — it renders as "new".
    """
    if base_stars is None:
        return []
    orphans = []
    for r in repos:
        full = r["full_name"]
        r["base_rank"] = (base_ranks or {}).get(full)
        before = base_stars.get(full)
        if before is None:
            orphans.append(full)
            continue
        r["delta_stars"] = r["stars"] - before
        r["pct_stars"] = (r["delta_stars"] / before * 100) if before else None
    return orphans


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


def collect_baselines(root: Path, generated_at: str, excluded: set[str],
                      render_count: int, trendcfg: dict) -> dict:
    """The momentum baseline: the newest committed repos.json at least
    `window_days` old, read out of git history.

    One snapshot feeds both `Pos` and `+Stars`, so the two columns always describe
    the same span. If it is unavailable — no git, a depth-1 clone, history shorter
    than the window — the miss is logged and both columns plus the Trending section
    drop out, and the README still renders.
    """
    window_days = int(trendcfg.get("window_days", 7))
    mom = {
        "window_days": window_days,
        "trending_size": int(trendcfg.get("trending_size", 5)),
        "base_at": "", "base_ranks": None, "base_stars": None,
    }
    ts = trend.parse_ts(generated_at)
    if ts is None:
        log(f"WARNING: unparseable generated_at {generated_at!r}; "
            f"momentum columns omitted.")
        return mom

    base = trend.snapshot_at(root, trend.shift_days(ts, window_days), generated_at)
    if not base:
        log(f"WARNING: no repos.json snapshot at least {window_days} days old in git "
            f"history (shallow clone?); 'Pos'/'+Stars' columns and "
            f"'{TRENDING_HEADING}' section omitted.")
        return mom

    mom["base_at"], universe = base
    mom["base_stars"] = trend.stars_map(universe)
    # Rank the historical universe with TODAY's exclusion sets: Pos should track real
    # star movement, not jump because filtered.json / scope_filter changed who is
    # eligible in between.
    mom["base_ranks"] = trend.rank_map(
        select_render_set(universe, excluded, render_count))
    return mom


def build_render_payload(repos: list[dict], generated_at: str) -> dict:
    """Slim on-disk record of the published selection: repo references in rank
    (stars-descending) order. Metadata (stars/description/topics) is NOT
    duplicated here — it lives in repos.json, the single source of truth."""
    return {
        "generated_at": generated_at,
        "generator": "helpers/render.py",
        "count": len(repos),
        "repos": [r["full_name"] for r in repos],
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
    # "|" is the only character that can break the cell; deliberately NOT cell(),
    # which would strip a leading "-" or "." and silently rename the repo.
    label = r["name"].replace("|", "\\|")
    return f"[{label}]({r['url']})"


# Column specs: (header, value function). Every table is assembled from these, so the
# leaderboard, the Trending cut and the per-category tables can't drift apart.
# Headers stay bare — the definitions live in docs/METHODOLOGY.md, not in the tables.
COL_RANK = ("#", lambda r: str(r["rank"]))
COL_NAME = ("Name", repo_link)
COL_STARS = ("Stars", lambda r: human_stars(r["stars"]))
COL_POS = ("Pos", lambda r: rank_move(r["base_rank"], r["rank"]))
COL_GAIN = ("+Stars", lambda r: human_delta(r["delta_stars"]))
COL_GROWTH = ("Growth", lambda r: human_pct(r["pct_stars"]))
COL_CATEGORY = ("Category", lambda r: cell(r["category"]))
COL_DESC = ("Description", lambda r: cell(r["description"]))


def table(repos: list[dict], cols: list[tuple]) -> str:
    """Markdown table from column specs. Every column is left-aligned (`:---`)."""
    lines = ["| " + " | ".join(h for h, _ in cols) + " |",
             "|" + "|".join(":---" for _ in cols) + "|"]
    for r in repos:
        lines.append("| " + " | ".join(fn(r) for _, fn in cols) + " |")
    return "\n".join(lines)


def repo_columns(mom: dict, with_category: bool) -> list[tuple]:
    """Columns for a published-repo table. The momentum pair appears only when the
    baseline was found, so a shallow clone renders the plain ranking."""
    cols = [COL_RANK, COL_NAME, COL_STARS]
    if mom.get("base_at"):
        cols += [COL_POS, COL_GAIN]
    if with_category:
        cols.append(COL_CATEGORY)
    cols.append(COL_DESC)
    return cols


# --------------------------------------------------------------------------
# static prose
# --------------------------------------------------------------------------

# The README body is kept to tables only; the prose that used to live here (scope,
# column definitions, disclaimer, licensing) moved to hand-maintained docs, and this
# footer is the single line that points at them.
FOOTER = """---

- [**Scope & methodology**](docs/METHODOLOGY.md) — how the list is built, and what
  each column means
- [**Contributing**](CONTRIBUTING.md) — which files are generated, and how to change
  what appears
- [**Disclaimer**](docs/DISCLAIMER.md) — no endorsement, third-party links,
  trademarks, removals, licensing
- [**License**](LICENSE) — CC0-1.0 for this repository; listed repos keep their own
"""


def build_readme(render_set: list[dict], generated_at: str, cfg: dict,
                 mom: dict) -> str:
    repos = parse_repos(render_set, cfg)
    orphans = annotate_momentum(repos, mom.get("base_ranks"), mom.get("base_stars"))
    if orphans:
        log(f"WARNING: {len(orphans)} published repo(s) absent from the "
            f"{mom['window_days']}-day baseline (new entrants?), shown as '—': "
            f"{', '.join(orphans)}")
    top_n = cfg.get("top_table_size", 30)

    top = repos[:top_n]
    tail = repos[top_n:]
    grouped = group_tail(tail, cfg) if tail else []
    movers = trend.top_movers(repos, mom["trending_size"]) if mom.get("base_at") else []

    out = [
        "# Awesome Claude Code & Agent Tools",
        "",
        "> The most-starred repositories in the Claude Code / skills / agents / MCP ecosystem.",
        f"> **Updated {fmt_date(generated_at)}** · {len(repos)} repositories by live "
        f"GitHub stars" + (f" · `Pos`/`+Stars` vs {fmt_date(mom['base_at'])}"
                           if mom.get("base_at") else ""),
    ]

    # table of contents — sections only; the static docs live outside the README
    top_heading = f"Top {len(top)}"
    out += ["", "## Contents", ""]
    if movers:
        out.append(f"- [{TRENDING_HEADING}](#{slugify(TRENDING_HEADING)})")
    out.append(f"- [{top_heading}](#{slugify(top_heading)})")
    if grouped:
        out.append(f"- [By category](#{slugify('By category')})")
        for heading, _ in grouped:
            out.append(f"  - [{heading}](#{slugify(heading)})")
    out.append("")

    if movers:
        out += [f"## {TRENDING_HEADING}", "",
                table(movers, [COL_RANK, COL_NAME, COL_STARS, COL_GAIN, COL_GROWTH]),
                ""]

    out += [f"## {top_heading}", "",
            table(top, repo_columns(mom, with_category=True)), ""]

    if grouped:
        out += ["## By category", ""]
        for heading, items in grouped:
            out += [f"### {heading}", "",
                    table(items, repo_columns(mom, with_category=False)), ""]

    out.append(FOOTER)
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

    # Filtered entries outside the universe are the expected steady state: an excluded
    # repo that slips under min_stars leaves repos.json but keeps its exclusion, ready
    # for when it climbs back. Report the count, not the names — a per-run wall of them
    # reads like rot. A *renamed* repo hides in this same bucket and does need fixing,
    # but that's only detectable online: see the CLASSIFICATION AUDIT in
    # .claude/skills/maintain-ranking-scripts/.
    universe_ids = {r.get("full_name", "").lower() for r in universe}
    dormant = filtered_ids - universe_ids
    if dormant:
        log(f"note: {len(dormant)} of {len(filtered_ids)} filtered.json entries are "
            f"outside the current universe (below the star floor, or renamed); audit "
            f"periodically for renames")

    # floor guards the PUBLISHED set, not the universe
    if len(render_set) < MIN_REPOS:
        log(f"FATAL: only {len(render_set)} repos in the render set (< {MIN_REPOS}); "
            f"refusing to overwrite README.")
        return 1

    generated_at = universe_doc.get("generated_at") or datetime.now(
        timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # momentum baselines come from git history (helpers/trend.py) — still offline
    mom = collect_baselines(HERE.parent, generated_at, excluded, render_count,
                            cfg["render"].get("trend", {}))
    render_payload = build_render_payload(render_set, generated_at)
    readme = build_readme(render_set, generated_at, cfg, mom)

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
