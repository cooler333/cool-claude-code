# maintain-ranking-scripts — reference

Deep reference for the ranking pipeline. Read this when making non-trivial
changes (schema, discovery, ranking, categorization).

## Pipeline overview

```
config.json ──▶ fetch.py ──▶ repos.json ──▶ render.py ──▶ README.md
```

- **fetch.py**: network only. discovery → metadata → deny/filter → rank/trim →
  raw README (for empty-description repos) → raw JSON. No categorize/brief.
- **render.py**: no network. JSON → categorize + brief → TOC + grouped markdown tables.

## config.json keys

- `target_count` — how many repos to keep (default 100).
- `min_stars` — floor; repos below are dropped (currently 20000).
- `scope_filter.any` — at least one substring must appear in name/desc/topics
  (the "is this in-ecosystem?" gate).
- `discovery.repo_search_queries` — GitHub search queries (topic:/keyword).
- `discovery.code_search_filenames` — filenames for `gh search code`.
- `discovery.awesome_lists` — CSV sources (repo + path + column).
- `alias_map` — rename map: `old/name` → `new/name` (applied before fetch).
- `denylist` — editorial exclude (case-insensitive). **No allowlist.** Reserved for
  non-Claude-compatible / single-vendor competing coding-agent CLIs and generic, non-AI
  repos that match by keyword but aren't ecosystem-specific (e.g. `sindresorhus/awesome`).
- `brief` — `max_chars`, `readme_excerpt_max_chars`, `readme_raw_max_chars`,
  `fetch_readme_when_description_empty` (fetch caps the raw README; render parses it).
- `category_rules` — `topic_map` (topic→category) + `keyword_rules` (ordered;
  `match_owner` or `any` substrings) + `default_category` (`"Other"`).
- `render.category_order` — the fixed, ordered category headings for the By-category
  section. **Must list every category `category_rules` can emit** (except the default
  `"Other"`), or that category silently falls into "Other".
- `render.other_category_label` / `render.other_max` — the catch-all label and the
  warn threshold (render logs a warning when "Other" ≥ this many repos).
- `http.user_agent` — sent on every request.

## repos.json schema contract

```json
{
  "generated_at": "ISO8601",
  "generator": "helpers/fetch.py",
  "schema_version": 2,
  "count": 97,
  "target_count": 100,
  "partial": true,
  "repos": [
    {
      "rank": 1,
      "full_name": "owner/name",
      "url": "https://github.com/owner/name",
      "stars": 123,
      "description": "raw GitHub description (UNtruncated)",
      "topics": ["x", "y"],
      "archived": false,
      "sources": ["repo_search", "code_search"],
      "redirected_from": null,
      "readme_raw": "first N chars of raw README, newlines preserved (empty unless description was empty)"
    }
  ]
}
```

- Raw snapshot: **no `category` or brief** — render derives both. `description` is the
  untruncated GitHub description; `readme_raw` is only populated when the description
  was empty (capped to `brief.readme_raw_max_chars`, newlines kept).
- `schema_version` — bump when shape changes; update both scripts + docs.
- Atomic write: fetch writes to a temp path then renames (write is the last step, so a
  good `repos.json` is never clobbered).
- Ranking lives entirely in fetch (`(-stars, full_name)`); render trusts `rank`.

## Editing recipes

- Add a discovery query → `repo_search_queries`. Re-run fetch; confirm new repos.
- Re-categorize / relabel → tweak `category_rules` (+ `render.category_order`); re-run
  **render only** (no fetch needed — category + brief are computed offline).
- Bump star floor → `min_stars`; re-run fetch.
- Change columns / TOC / category order → edit `render.py` / `render.category_order`;
  re-run render.
- Exclude a competing CLI → add it to `denylist`; re-run fetch.

## Scope philosophy

- In: Claude Code skills/plugins/agents, MCP servers/clients, agent harnesses,
  memory/context tools; general-purpose & provider-neutral LLM/agent frameworks; and
  multi-model coding-agent CLIs that support Claude (opencode, OpenHands, goose).
- Out: single-vendor / non-Claude-compatible competing coding-agent CLIs
  (`google-gemini/gemini-cli`, `openai/codex` — in `denylist`); generic non-AI
  awesome-of-awesomes (`sindresorhus/awesome` — in `denylist`); general chat UIs.
  Archived repos and repos under `min_stars` are filtered out automatically.

## Common failure modes

- **Rate limit** → fetch exits 2; CI fails; no write. Re-run later.
- **Low coverage** (< floor_fraction) → fetch refuses to overwrite. Investigate.
- **Empty/changed schema** → render's floor check refuses to overwrite README.
- **gh not authed** → code search skipped (logged), other signals still run.
- **"Other" too big / category drift** → render warns (non-fatal); add a category
  rule and/or a `category_order` entry.

## Notes

- Background jobs / multiple writers onto `repos.json` can race — don't run two
  fetches at once.
- Keep this file and `SKILL.md` in sync with actual script behavior.
