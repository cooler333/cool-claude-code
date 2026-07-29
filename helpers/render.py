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
            # filled by annotate_momentum; None = no baseline for this repo
            "prev_rank": None,
            "delta_stars": None,
            "pct_stars": None,
        })
    return repos


def annotate_momentum(repos: list[dict], prev_ranks: dict | None,
                      week_stars: dict | None) -> list[str]:
    """Attach rank movement and star growth in place; return the repos that had no
    star baseline (new entrants — their gain is unknown, not zero).

    Either baseline may be absent entirely (short or shallow history); the matching
    fields then stay None and their whole column is dropped from the render. A repo
    missing from the rank baseline is not an orphan — it renders as "new".
    """
    no_baseline = []
    for r in repos:
        full = r["full_name"]
        if prev_ranks is not None:
            r["prev_rank"] = prev_ranks.get(full)
        if week_stars is not None:
            before = week_stars.get(full)
            if before is None:
                no_baseline.append(full)
            else:
                r["delta_stars"] = r["stars"] - before
                r["pct_stars"] = (r["delta_stars"] / before * 100) if before else None
    return no_baseline


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
    """Momentum baselines: the previous refresh (rank movement) and the snapshot from
    `window_days` ago (star growth), both read out of git history.

    Both are optional. Anything unavailable — no git, a depth-1 clone, history
    shorter than the window — is logged and drops only its own column, so the README
    always renders.
    """
    window_days = int(trendcfg.get("window_days", 7))
    mom = {
        "window_days": window_days,
        "trending_size": int(trendcfg.get("trending_size", 5)),
        "prev_ranks": None, "prev_at": "",
        "week_stars": None, "week_at": "",
    }
    ts = trend.parse_ts(generated_at)
    if ts is None:
        log(f"WARNING: unparseable generated_at {generated_at!r}; "
            f"momentum columns omitted.")
        return mom

    prev = trend.snapshot_before(root, generated_at)
    if prev:
        mom["prev_at"], prev_universe = prev
        # Rank the historical universe with TODAY's exclusion sets: Δ pos should
        # track real star movement, not jump because filtered.json / scope_filter
        # changed who is eligible in between.
        mom["prev_ranks"] = trend.rank_map(
            select_render_set(prev_universe, excluded, render_count))
    else:
        log("WARNING: no earlier repos.json snapshot in git history (shallow "
            "clone?); 'Δ pos' column omitted.")

    week = trend.snapshot_at(root, trend.shift_days(ts, window_days), generated_at)
    if week:
        mom["week_at"], week_universe = week
        mom["week_stars"] = trend.stars_map(week_universe)
    else:
        log(f"WARNING: no repos.json snapshot at least {window_days} days old in git "
            f"history (shallow clone?); '+stars' column and '{TRENDING_HEADING}' "
            f"section omitted.")
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
# Top-N leaderboard and the per-category tables can't drift apart.
COL_RANK = ("#", lambda r: str(r["rank"]))
COL_NAME = ("Name", repo_link)
COL_STARS = ("Stars", lambda r: human_stars(r["stars"]))
COL_MOVE = ("Δ pos (1d)", lambda r: rank_move(r["prev_rank"], r["rank"]))
COL_CATEGORY = ("Category", lambda r: cell(r["category"]))
COL_DESC = ("Description", lambda r: cell(r["description"]))
COL_PCT = ("Growth", lambda r: human_pct(r["pct_stars"]))


def col_gain(window_days: int) -> tuple:
    return (f"+stars ({window_days}d)", lambda r: human_delta(r["delta_stars"]))


def table(repos: list[dict], cols: list[tuple]) -> str:
    """Markdown table from column specs. Every column is left-aligned (`:---`)."""
    lines = ["| " + " | ".join(h for h, _ in cols) + " |",
             "|" + "|".join(":---" for _ in cols) + "|"]
    for r in repos:
        lines.append("| " + " | ".join(fn(r) for _, fn in cols) + " |")
    return "\n".join(lines)


def repo_columns(mom: dict, with_category: bool) -> list[tuple]:
    """Columns for a published-repo table. The momentum columns appear only when
    their baseline was found, so a shallow clone renders the plain ranking."""
    cols = [COL_RANK, COL_NAME, COL_STARS]
    if mom.get("prev_at"):
        cols.append(COL_MOVE)
    if mom.get("week_at"):
        cols.append(col_gain(mom["window_days"]))
    if with_category:
        cols.append(COL_CATEGORY)
    cols.append(COL_DESC)
    return cols


def momentum_note(mom: dict) -> str:
    """One line telling the reader which window each delta column covers — they
    differ (position is day-over-day, stars are weekly), and the baselines are real
    snapshots, so the actual timestamps are named rather than implied."""
    parts = []
    if mom.get("prev_at"):
        parts.append(f"**Δ pos** — places gained since the previous refresh "
                     f"({fmt_date(mom['prev_at'])})")
    if mom.get("week_at"):
        parts.append(f"**+stars** — stars gained since {fmt_date(mom['week_at'])}")
    if not parts:
        return ""
    return ("_" + "; ".join(parts) +
            ". Both are diffed against committed `helpers/repos.json` snapshots._")


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
- **Floor:** repositories under {min_stars} stars, and archived repositories, are excluded.
- **Star counts** are a point-in-time snapshot and drift daily.
- **Momentum (`Δ pos`, `+stars`, Trending):** diffed against older committed
  `helpers/repos.json` snapshots in this repo's git history — position against the
  previous refresh, stars over the last {window_days} days. No extra API calls; the
  exact baseline timestamps are printed under the tables. A clone without history
  simply renders without those columns.
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


def build_readme(render_set: list[dict], generated_at: str, cfg: dict,
                 mom: dict) -> str:
    repos = parse_repos(render_set, cfg)
    orphans = annotate_momentum(repos, mom.get("prev_ranks"), mom.get("week_stars"))
    if orphans:
        log(f"WARNING: {len(orphans)} published repo(s) absent from the "
            f"{mom['window_days']}-day baseline (new entrants?), shown as '—': "
            f"{', '.join(orphans)}")
    top_n = cfg.get("top_table_size", 30)
    generated_at = fmt_date(generated_at)

    top = repos[:top_n]
    tail = repos[top_n:]
    grouped = group_tail(tail, cfg) if tail else []
    note = momentum_note(mom)
    movers = trend.top_movers(repos, mom["trending_size"]) if mom.get("week_at") else []

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
    if movers:
        out.append(f"- [{TRENDING_HEADING}](#{slugify(TRENDING_HEADING)})")
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

    out += ["", f"## {top_heading}", ""]
    if note:
        out += [note, ""]
    out += [table(top, repo_columns(mom, with_category=True)), ""]

    if movers:
        out += [
            f"## {TRENDING_HEADING}",
            "",
            f"_The `+stars` column, sorted: the biggest star gains of the last "
            f"{mom['window_days']} days (since {fmt_date(mom['week_at'])}) among the "
            f"{len(repos)} published repositories. They also appear in the tables "
            f"above — this is a shortcut, not a separate ranking._",
            "",
            table(movers, [COL_RANK, COL_NAME, COL_STARS,
                           col_gain(mom["window_days"]), COL_PCT]),
            "",
        ]

    if grouped:
        out += [
            "## By category",
            "",
            "_The long tail below the top leaderboard, grouped by category "
            "(top-ranked repos above are not repeated here). `#` is the overall "
            "rank._",
            "",
        ]
        if note:
            out += [note, ""]
        for heading, items in grouped:
            out += [f"### {heading}", "",
                    table(items, repo_columns(mom, with_category=False)), ""]

    scope = SCOPE.format(min_stars=f"{cfg.get('min_stars', 20000):,}",
                         window_days=mom["window_days"])
    out += [scope, CONTRIBUTING, DISCLAIMER, LICENSE]
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
