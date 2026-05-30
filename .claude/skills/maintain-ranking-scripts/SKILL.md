---
name: maintain-ranking-scripts
description: Maintain and improve the scripts that auto-generate this repo's star-ranked "Awesome Claude Code" README. Use when editing helpers/fetch.py, helpers/render.py, or helpers/config.json — improving discovery queries, adding a category rule, changing the repos.json schema or README layout — and when running the web-research COVERAGE CHECK to find repos the deterministic discovery missed. Does NOT run the daily refresh (CI cron does that) and does NOT hand-curate a list (discovery is algorithmic).
---

# maintain-ranking-scripts

Maintain the scripts behind this repo's auto-generated, star-ranked README of the
Claude Code / skills / agents / MCP ecosystem.

## What this repo's pipeline does

1. **`helpers/fetch.py`** (network only) discovers candidate repos (GitHub repo
   search + `gh` code search + an awesome-list CSV), resolves authoritative
   metadata via the GraphQL API (REST fallback), drops denylisted repos (read from
   `helpers/out_of_scope.json`), filters
   (min_stars + scope + not archived), ranks by stars, fetches raw README text for
   repos that have no description, and serializes the **raw** full kept set (every
   in-scope repo, not just the top N) to **`helpers/repos.json`**. It does **not**
   categorize or write briefs.
2. **`helpers/render.py`** (no network) reads that JSON and regenerates
   **`README.md`**: it **categorizes**, builds the short **briefs** (from the saved
   description / raw README / topics), and lays out a Table of Contents, the Top-N
   leaderboard, and the long tail split into predefined per-category tables.
3. **`.github/workflows/refresh-ranking.yml`** runs the two daily and commits
   the regenerated `README.md` + `helpers/repos.json` to `main`.

The scripts are **stdlib-only** (no pip installs) and must stay that way.

## Working on this

- **Tune what's discovered** — edit `helpers/config.json` (`repo_search_queries`,
   `code_search_filenames`, `awesome_lists`, `min_stars`). Note `target_count` is no
   longer a cap — `repos.json` keeps the full in-scope set; `target_count` is only the
   `partial` health threshold (see `reference.md`).
- **Improve categorization** — adjust `category_rules` (topic_map / keyword_rules)
   in `config.json`; `render.py` assigns the category and groups by it. If you add a
   new category, also add its heading to `render.category_order` (they must stay in
   sync, or the category falls into "Other").
- **Change the table layout / columns / Table of Contents** — edit `helpers/render.py`.
- **Add/repair an alias** (renamed/moved repos) — `alias_map` in `config.json`.
- **Adjust scope** (what counts as in-ecosystem) — `scope_filter` in `config.json`.
- **Exclude an unwanted repo** — add a `{ "repo_id": "owner/name", "reason": "..." }`
   entry to the `out_of_scope` array in `helpers/out_of_scope.json` (its own file,
   loaded by `fetch.py` from next to `config.json`; the reader also tolerates a bare
   string). This denylist is reserved for editorial exclusions that rules can't
   detect: repos that match scope keywords but aren't Claude Code ecosystem tools.
   The established categories are:
   1. **Competing coding-agent CLIs / editors** — single-vendor or non-Claude-compatible
      (e.g. `google-gemini/gemini-cli`, `openai/codex`, `voideditor/void`).
   2. **API gateways / proxies / resellers** — model-access infrastructure, not a
      Claude Code tool (e.g. `songquanpeng/one-api`, `Wei-Shaw/sub2api`,
      `chatanywhere/GPT_API_free`, `BerriAI/litellm`, `QuantumNous/new-api`).
   3. **Generic AI apps / clients / platforms** — chat UIs, low-code/no-code builders,
      "AI second brain" apps that aren't ecosystem-specific (e.g. `jeecgboot/JeecgBoot`,
      `danny-avila/LibreChat`, `chatboxai/chatbox`, `khoj-ai/khoj`, `labring/FastGPT`).
   4. **Generic non-AI repos** that match only by keyword (e.g. `sindresorhus/awesome`,
      `tw93/Pake`).
   5. **Leaked / rights-infringing content** — repos that primarily republish
      extracted/leaked proprietary system prompts, credentials, or closed-source
      material (e.g. `asgeirtj/system_prompts_leaks`,
      `x1xhlol/system-prompts-and-models-of-ai-tools`). Excluded as a legal/editorial
      precaution even when popular.

   There is **no allowlist**: if a clearly-relevant repo is missing, fix discovery (a
   query / scope term), never a hand-maintained include list.
- **Find repos discovery missed** — run the COVERAGE CHECK below.

## Critical constraints

- `helpers/fetch.py` — discovery + metadata + selection; reads `helpers/config.json`
   + `helpers/out_of_scope.json`, writes `helpers/repos.json`. Networked. Ends at raw
   data (no categorize/brief).
- `helpers/render.py` — reads `repos.json`, categorizes + briefs + regenerates
   `README.md`. **Never networks.**
- Discovery must stay **algorithmic**, not a hand-maintained allowlist of repos.
   The `denylist` is the only editorial lever, for off-scope repos that match by
   keyword: competing CLIs/editors, API gateways/proxies/resellers, generic AI
   apps/clients, generic non-AI repos, and leaked/rights-infringing content (see the
   five categories above).
- Keep scripts **stdlib-only** — no third-party packages.
- Preserve separation: `fetch.py` only writes `repos.json`; `render.py` only reads
   it and writes `README.md`.
- Any `repos.json` schema change must update the `fetch.py` writer, the `render.py`
   reader, AND this doc + `reference.md` in lockstep.
- The pipeline must stay **idempotent** and **deterministic**: same inputs →
   byte-identical `repos.json` and `README.md`.
- `render.py` must remain safe to re-run anytime (no network, atomic write).

## COVERAGE CHECK (web research)

When asked to audit coverage (find repos the deterministic pipeline missed):

1. Read `helpers/config.json` and the current `helpers/repos.json`.
2. Search the web / GitHub for active Claude Code/skills/MCP/agent repos.
3. For each candidate, check it against the live GitHub API (stars, pushed_at,
   archived) and the current ranking; collect those missing or mis-ranked.
4. Propose concrete `config.json` edits (queries, `alias_map`, scope terms, category
   rules; or a `denylist` entry to remove an off-scope repo — competing CLI, gateway/
   reseller, or generic app) — never hand-edit `README.md` or `repos.json`, and never
   add an allowlist.

## Notes

- The repo intentionally has **no Python deps**; don't add a `requirements.txt`.
- If you change the JSON schema, bump `schema_version` and any docs that describe it.
- `render.py` has a min-repo floor; respect it when changing thresholds. It also
  warns (non-fatal) when the "Other" category reaches `render.other_max` repos —
  that's a signal to refine `category_rules` / `category_order`.
- Rate limits are real; `ghclient.py` paces calls. Don't remove the throttling.
