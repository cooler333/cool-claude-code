---
name: maintain-ranking-scripts
description: >-
  Maintain and improve the scripts that auto-generate this repo's star-ranked
  "Awesome Claude Code" README. Use when editing helpers/fetch.py,
  helpers/render.py, or helpers/config.json — improving discovery queries, adding
  a category rule, changing the repos.xml schema or README layout — and when
  running the web-research COVERAGE CHECK to find repos the deterministic
  discovery missed. Does NOT run the daily refresh (CI cron does that) and does
  NOT hand-curate a list (discovery is algorithmic).
---

# Maintain ranking scripts

This repo publishes a star-ranked README of the Claude Code ecosystem,
regenerated automatically every day. There are exactly **two execution paths**:

1. **This skill (workstation)** edits the *scripts/config* (`helpers/fetch.py`,
   `helpers/render.py`, `helpers/config.json`) — including the automated
   web-research coverage check below. It commits to a **feature branch only**.
2. **CI** (`.github/workflows/refresh-ranking.yml`, daily cron) runs the scripts
   and commits the regenerated `README.md` + `helpers/repos.xml` to `main`.

This skill **never edits `README.md` directly** and **never pushes to `main`** —
that is CI's lane. It improves *how* discovery/ranking/rendering work; the next
CI run propagates the improvement into the README.

## When to use
- Improve/extend discovery (add a search query, topic, or awesome-list source).
- Add or reorder a category rule, or fix a miscategorization.
- Change the `repos.xml` schema or the README layout (tables, columns, Top-N).
- **Run the coverage check** — "are we missing popular repos?" (see below).
- Not for: refreshing stars (CI does it daily) or hand-adding one repo (instead,
  add it to `config.json` `allowlist`, or fix the discovery rule that missed it).

## File map
- `helpers/fetch.py` — discovery + ranking; writes `helpers/repos.xml`. Networked.
- `helpers/render.py` — reads `repos.xml`, regenerates `README.md`. **Never networks.**
- `helpers/ghclient.py` — auth + rate-limit/backoff + batched GraphQL.
- `helpers/config.json` — the main lever: queries, `allowlist`/`denylist`,
  `alias_map`, `category_rules`, render/fetch knobs.
- `.github/workflows/refresh-ranking.yml` — the daily cron.
- `reference.md` — scope, discovery signals + limits, schema contract, gotchas.

## Editing safely
- **stdlib only** — no `pip install`, no third-party imports. (`gh` CLI is an
  allowed *binary* dependency: used for `gh auth token` and the code-search signal.)
- Token comes from `GITHUB_TOKEN`/`GH_TOKEN`, else `gh auth token` — same as CI.
- Keep the contracts: `fetch.py` exits non-zero on hard failure (auth, rate-limit
  exhaustion, coverage below `fetch.floor_fraction`) so CI never renders bad data;
  `render.py` refuses to overwrite below `MIN_REPOS`.
- Preserve separation: `fetch.py` only writes `repos.xml`; `render.py` only reads
  it and writes `README.md`. Don't make `render.py` touch the network.
- Throttle: secondary rate limits (not the 5000/hr budget) are the real risk —
  keep the per-endpoint spacing and `Retry-After` handling in `ghclient.py`.
- Any `repos.xml` schema change must update the `fetch.py` writer, the `render.py`
  reader, and `reference.md` §Schema together.

## Test locally without hammering the API
- `python helpers/render.py --dry-run` — render the committed `repos.xml`, no write.
- `python helpers/fetch.py --limit 20 --dry-run` — small live run, prints XML,
  skips the floor check. Use `--no-readme` to skip README-excerpt fetches.
- Iterate on `render.py` against the existing `repos.xml` — zero network.

## Verify coverage via web research (automated procedure)
Run all steps in one invocation; it edits scripts/config only.
1. **Search the web** (WebSearch; escalate to the `deep-research` skill for depth):
   "best Claude Code skills/plugins", "awesome claude code", "top MCP servers",
   "Claude agent frameworks", plus recent Reddit/HN/blog roundups and Anthropic posts.
2. **Extract candidate repos** from results; **fetch & read repo content**
   (WebFetch / `gh`) — READMEs often link related tools — to surface adjacents.
3. **Diff vs `helpers/repos.xml`** (Read it): which popular/mentioned repos are absent?
4. **Diagnose each miss**: no matching topic · generic name uncaught by code search ·
   below `min_stars` · excluded by scope/`denylist` · or genuinely out of scope.
5. **Edit `helpers/config.json`** to close real gaps (new `repo_search_queries`/
   topics, `allowlist` entry, `alias_map` fix, or a `category_rule`). Touch
   `fetch.py` only if a new *signal type* is needed.
6. **Verify**: `python helpers/fetch.py --limit 30 --dry-run` and confirm the
   previously-missed repos now appear (note which `<discovery><source>` surfaces each).
7. **Commit** the config/script change on a feature branch; open a PR. Do not push to `main`.

## Definition of done
- [ ] Scripts stay stdlib-only and run under any Python 3.x.
- [ ] `fetch.py` keeps its non-zero-exit-on-hard-failure + `floor_fraction` contract.
- [ ] `render.py` keeps its `MIN_REPOS` floor before overwrite.
- [ ] `--dry-run` / `--limit` paths work for cheap local testing.
- [ ] Any schema change is reflected in `fetch.py`, `render.py`, and `reference.md`.
- [ ] Changes committed to a feature branch (never pushed to `main` by this skill).
