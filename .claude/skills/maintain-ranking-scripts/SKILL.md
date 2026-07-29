---
name: maintain-ranking-scripts
description: Maintain and improve the scripts that auto-generate this repo's star-ranked "Awesome Claude Code" README. Use when editing helpers/fetch.py, helpers/render.py, or helpers/config.json — tuning scope/category rules, changing the repos.json schema or README layout — and when running the CLASSIFICATION AUDIT to curate helpers/filtered.json and fix scope_filter false positives. Does NOT run the daily refresh (CI cron does that) and does NOT hand-curate an include list (discovery is an exhaustive sweep).
---

# maintain-ranking-scripts

Maintain the scripts behind this repo's auto-generated, star-ranked README of the
Claude Code / skills / agents / MCP ecosystem.

## What this repo's pipeline does

1. **`helpers/fetch.py`** (network only) does an **exhaustive** star-bucketed sweep
   of every public repo at or above `min_stars` (the Search API caps each query at
   1,000 results, so the star range is split into adaptive buckets, then
   concatenated). Because the sweep is exhaustive, **discovery is complete by
   construction** — there are no scoped queries that could miss a popular repo. It
   writes two files and nothing else:
   - **`helpers/repos.json`** — the full universe (every repo ≥ `min_stars`),
     metadata only. The single source of truth for everything downstream.
   - **`helpers/out_of_scope.json`** — AUTO classification: universe entries that
     **fail** `scope_filter` (not AI/Claude). Regenerated every run; never
     hand-edited.
   It does **not** categorize, write briefs, rank-to-top-N, fetch READMEs, or apply
   any denylist.
2. **`helpers/render.py`** (no network) selects the published set —
   `repos.json` minus `out_of_scope.json` minus `helpers/filtered.json` minus
   archived, top `render.render_count` by stars — writes
   **`helpers/repos_to_render.json`**, then **categorizes**, builds the short
   **briefs** (from description / topics), and lays out **`README.md`**: a Table of
   Contents, the Top-N leaderboard, a **Trending this week** cut, and the long tail
   split into per-category tables.
3. **`helpers/trend.py`** (no network, git only) supplies the **momentum** columns:
   it reads *older committed versions of `repos.json`* straight out of git history —
   the previous refresh (for `Δ pos`) and the snapshot from `render.trend.window_days`
   ago (for `+stars` and Trending). No extra API calls and no new state file; the
   daily commits **are** the star history. Every read is best-effort: no git, a
   depth-1 clone, or history shorter than the window drops the affected column and
   the README still renders.
4. **CI** (`.github/workflows/`): `refresh-ranking.yml` runs the sweep+render daily
   and commits the artifacts; `render-on-edit.yml` re-renders (no network) when a
   human edits `filtered.json` (NOT `config.json` — scope/min_stars are sweep-time,
   so config edits wait for the next sweep). **Both check out with `fetch-depth: 0`**
   — the default depth-1 clone would silently render without the momentum columns.

The ≥`min_stars` universe partitions as `repos.json` ⊇ (`out_of_scope.json` ∪
`filtered.json`). See `reference.md` for the full file contracts and schema.

The scripts are **stdlib-only** (no pip installs) and must stay that way.

## The two exclusion sets (orthogonal)

- **`out_of_scope.json` — AUTO, CI-owned.** "Not AI/Claude." Derived from
  `repos.json` + `scope_filter` every run. **Never hand-edit it** — if something is
  misclassified, fix the *rules* in `config.json` (single source of truth).
- **`filtered.json` — MANUAL, human-owned.** Repos that *are* AI/Claude-adjacent
  (they pass scope) but are excluded as **redundant**. Each entry is
  `{ "repo_id": "owner/name", "reason": "..." }` (a bare string is also tolerated).
  This is the **only** editorial lever; there is **no allowlist**. Established
  categories:
  1. **Competing coding-agent CLIs / editors** — single-vendor / non-Claude
     (e.g. `google-gemini/gemini-cli`, `openai/codex`, `voideditor/void`).
  2. **API gateways / proxies / resellers** (e.g. `songquanpeng/one-api`,
     `BerriAI/litellm`, `QuantumNous/new-api`).
  3. **Generic AI apps / clients / platforms** — chat UIs, low/no-code builders,
     "AI second brain" apps (e.g. `danny-avila/LibreChat`, `khoj-ai/khoj`).
  4. **Leaked / rights-infringing content** — republished proprietary system
     prompts, credentials, closed-source material (e.g.
     `asgeirtj/system_prompts_leaks`). Excluded on legal/editorial grounds.

  (Generic **non-AI** repos that match only by keyword belong in neither file —
  they fail `scope_filter` and land in `out_of_scope.json` automatically.)

## Working on this

- **Adjust scope** (what counts as in-ecosystem) — `scope_filter` in `config.json`.
  This drives `out_of_scope.json`. Loosen a term to rescue a false positive; tighten
  to push a keyword-only match out. **`scope_filter` and `min_stars` are applied by
  `fetch.py` during the sweep, not by render** — editing them takes effect on the next
  daily sweep (or a manual `fetch.py` run / a `refresh-ranking` `workflow_dispatch`).
  A push to `config.json` does NOT trigger the on-edit re-render (that would publish a
  README that ignores the change); only `filtered.json` edits re-render instantly.
- **Improve categorization** — `category_rules` (`topic_map` / `keyword_rules`) in
  `config.json`; `render.py` assigns the category and groups by it. If you add a new
  category, also add its heading to `render.category_order` (must stay in sync, or it
  falls into "Other").
- **Change how many are published** — `render.render_count` in `config.json`.
- **Change the momentum window / Trending size** — `render.trend.window_days` and
  `render.trend.trending_size` in `config.json`. `window_days` drives both the
  `+stars` column and the Trending cut; `Δ pos` is always "since the previous
  refresh" and has no knob.
- **Change the table layout / columns / Table of Contents** — `helpers/render.py`.
  All tables are assembled from the shared `COL_*` specs and `table()`, so add or
  reorder a column there once instead of per table.
- **Exclude an AI-adjacent-but-redundant repo** — add a `{ repo_id, reason }` entry
  to `helpers/filtered.json`.
- **Audit classification** — run the CLASSIFICATION AUDIT below.

## Critical constraints

- `helpers/fetch.py` — exhaustive sweep + auto scope classification; reads
  `helpers/config.json`, writes `helpers/repos.json` + `helpers/out_of_scope.json`.
  Networked. No categorize/brief/rank-to-N/README.
- `helpers/render.py` — reads `repos.json` + `out_of_scope.json` + `filtered.json`,
  selects the top-N, writes `repos_to_render.json` + `README.md`. **Never networks.**
- `helpers/trend.py` — the **only** git-reading module, as `ghclient.py` is the only
  networking one. Keep git out of `render.py`; keep `trend.py` best-effort (it returns
  `None` instead of raising, so a missing baseline degrades a column rather than
  failing a run). It reads nothing but committed `repos.json` blobs and writes nothing.
- Discovery stays **algorithmic** (an exhaustive sweep), never a hand-maintained
  allowlist. `filtered.json` is the only editorial lever.
- Keep scripts **stdlib-only** — no third-party packages.
- Preserve separation: `fetch.py` writes only `repos.json`/`out_of_scope.json`;
  `render.py` reads those + `filtered.json` and writes only
  `repos_to_render.json`/`README.md`.
- Any `repos.json` schema change must update the `fetch.py` writer, the `render.py`
  reader, AND this doc + `reference.md` in lockstep.
- The pipeline stays **idempotent** and **deterministic**: same inputs → identical
  outputs (modulo `generated_at`).
- `render.py` must remain safe to re-run anytime (no network, atomic write).
- The big generated files (`repos.json`, `out_of_scope.json`) are committed and have
  a **single writer** (the daily job); don't add a second writer. Rate limits are
  real — `ghclient.py` paces calls; don't remove the throttling.

## CLASSIFICATION AUDIT

The sweep is exhaustive, so there is nothing to "discover" — every repo ≥ `min_stars`
is already in `repos.json`. Auditing means checking that each repo is in the *right*
bucket. **`grep` the large files; don't read them whole.** Note that
`out_of_scope.json` and `repos_to_render.json` hold only repo references (`repo_id` /
`full_name`); their stars/description/topics live in `repos.json` — join on the name
(`grep` the id in `repos.json`) when you need the metadata to judge a repo.

1. **Scope false positives** (ecosystem repos wrongly auto-excluded): scan
   `out_of_scope.json` for repo names that look genuinely Claude/agent/MCP-related,
   then check their description/topics in `repos.json`. Fix by loosening/adding a
   `scope_filter` term in `config.json` (never hand-edit `out_of_scope.json`).
2. **Scope false negatives** (off-topic repos that slipped into the published set):
   inspect `repos_to_render.json` (join `repos.json` for metadata); if a keyword-only
   match shouldn't be in scope, tighten `scope_filter`.
3. **Editorial redundancy** (AI-adjacent but not worth listing): add the repo to
   `helpers/filtered.json` with a `reason`.
4. Never hand-edit `README.md`, `repos.json`, `out_of_scope.json`, or
   `repos_to_render.json`, and never add an allowlist.

## Notes

- The repo intentionally has **no Python deps**; don't add a `requirements.txt`.
- If you change the `repos.json` schema, bump `schema_version` and the docs.
- `render.py` has a min-repo floor on the **published set**; respect it. It also
  warns (non-fatal) when "Other" reaches `render.other_max` — refine
  `category_rules` / `category_order`.
