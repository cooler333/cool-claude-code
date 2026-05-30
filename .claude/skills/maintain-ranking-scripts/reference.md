# Reference — maintain-ranking-scripts

Deep methodology for the ranking pipeline. SKILL.md stays lean; load this when
editing internals.

## Scope

**Root problem.** Keyword/topic search only finds repos that *self-describe* as
Claude-related; it misses high-value, generically named tools (e.g.
`anthropics/skills`, `modelcontextprotocol/servers`, `earendil-works/pi`). Blog
"best-of" posts share the bias and report stale stars. So discovery combines
name-independent signals, and stars are always re-verified live.

- **In scope:** Claude Code skills, agents, plugins, harnesses, memory and
  orchestration; the MCP tool-protocol layer (servers, directories, switchers);
  directly adjacent coding-agent CLIs (opencode, codex, gemini-cli, OpenHands, goose).
- **Out of scope:** general-purpose AI frameworks not Claude/skill/agent-specific
  (`langchain`, `dify`, `langflow`, `firecrawl`, `ragflow` — in `denylist`); general chat UIs.

## Discovery signals + rate limits
`fetch.py` unions three signals, dedupes (alias map), then re-ranks by live stars.
1. **Repo search** — REST `/search/repositories?q=…&sort=stars` (`topic:claude-code`,
   keyword queries). Sortable by stars; ~30 req/min authenticated; paged.
2. **Structural code search** — `gh search code --filename SKILL.md|CLAUDE.md`
   (name-independent). **Use `gh`, not raw `/search/code`** — the REST endpoint
   422s on qualifier-only queries. 1000-result cap, ~10 req/min, can't sort by
   stars → discovery signal only. Best-effort; a failure must not fail the run.
3. **Awesome-list aggregation** — harvest `hesreallyhim/awesome-claude-code`
   (`data/repo-ticker.csv`) via the content API with `Accept: application/vnd.github.raw`.
   Names only; the CSV stars are **stale** — discard them, re-verify live.

## Authoritative star counts
- One **batched GraphQL** query, ~50 aliased `repository(owner,name){…}` fields
  (`r0…r49`), mapped back **by alias index**. Cuts hundreds of lookups to a few requests.
- Validate each `owner/name` against GitHub's charset before building the query
  (owner `^[A-Za-z0-9-]+$`, name `^[A-Za-z0-9._-]+$`): one bad name breaks the
  whole batch and is an injection vector.
- GraphQL returns `null` for renamed/deleted repos → **REST fallback**
  `/repos/{owner}/{name}` (follows redirects); record `redirected_from` and
  **re-dedupe** by canonical `full_name` afterward (a redirect can collapse two
  candidates into one).

## Heuristic categorization (config-driven, first-match-wins)
Order: **owner override → `topic_map` → ordered `keyword_rules` (over
name+description+topics) → `default_category`.** All data lives in
`config.json.category_rules`; `fetch.py` only evaluates it. Add rules in priority
order (earlier = higher priority). `<category source="...">` records which stage
fired (`topic_map` / `keyword_rules` / `default`).

## Brief fallback chain (requirement: never blank without trying)
`description` → (if empty) README first meaningful paragraph via
`/repos/{o}/{n}/readme` (raw bytes; skip headings/badges/HTML/blockquotes, strip
markdown, truncate) → joined `topics[:5]` → empty (`render.py` shows `—`). README
is fetched **only** when the description is empty, capping extra requests.
`<description source="...">` records the provenance.

## repos.xml schema contract
Root: `<repos generated_at="ISO-8601-UTC Z" generator schema_version count
target_count partial>`. Each `<repo rank="N">` has, in order: `full_name`, `url`,
`stars` (raw int — humanized in render), `category source=…`, `description
source=…`, `topics/topic*` (sorted), `discovery/source*` (sorted), `archived`,
optional `redirected_from`. Determinism (clean diffs): stable sort `(-stars,
full_name)`, sorted topics/sources, fixed attribute order, atomic write. Any
change here must update `fetch.py` writer + `render.py` reader + this section together.

## Failure / write policy
- Rate-limit exhaustion or transport error → exception → non-zero exit, **no write**
  (write is the last step, so good `repos.xml` is never clobbered).
- `fetch.floor_fraction` (default 0.9): if metadata `coverage` (definitively
  resolved-or-404 ÷ attempted) drops below it → hard fail, no write. Distinguishes
  a rate-limited truncation from a legitimately small ecosystem.
- `render.py` refuses to overwrite README below `MIN_REPOS` (10).
- CI commits only on a real diff and tags `[skip ci]`; pushes by `GITHUB_TOKEN`
  don't re-trigger workflows.

## Alias / dedup map (in `config.json.alias_map`)
| Seen as | Canonical |
|---------|-----------|
| `All-Hands-AI/OpenHands` | `OpenHands/OpenHands` |
| `affaan-m/ECC` | `affaan-m/everything-claude-code` |
| `sst/opencode` | `anomalyco/opencode` |
| `getAsterisk/claudia` | `winfunc/opcode` |
| `forrestchang/andrej-karpathy-skills` | `multica-ai/andrej-karpathy-skills` |

## Gotchas
- **`gh` for code search** (not raw REST) — qualifier-only `/search/code` 422s.
- **GraphQL null on rename** → REST fallback follows redirects.
- **Code search**: 1000-cap, ~10/min, no star sort — discovery only, throttle.
- **Secondary (anti-abuse) rate limits** are the real risk, not the 5000/hr
  primary budget — keep per-endpoint spacing + honor `Retry-After`/`429`.
- **Content API base64**: request raw bytes (`Accept: application/vnd.github.raw`)
  rather than decoding base64 (the legacy bash hit macOS `base64 -d` issues).
- **No shared-file races**: write to `*.tmp` then atomic-rename once; never
  background multiple writers onto `repos.xml`.
- **Branch protection**: the CI daily push needs `main` to allow
  `github-actions[bot]`; otherwise switch the workflow to a PR flow.
