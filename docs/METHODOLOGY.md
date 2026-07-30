# Scope & methodology

How [`README.md`](../README.md) is built. It is generated, once per day, by
`helpers/fetch.py` → `helpers/render.py` (see
`.github/workflows/refresh-ranking.yml`) — never edited by hand.

## Reading the tables

| column | meaning |
|:---|:---|
| `#` | overall rank in the published set, by live star count |
| `Name` | the repository's short name, linked to GitHub. The owner is shown in parentheses only when two published repos share a short name |
| `Stars` | stargazers at the last refresh |
| `Pos` | places gained since the baseline snapshot — `—` if unchanged, `new` if the repo was not in the published set then |
| `+Stars` | stars gained since the baseline snapshot |
| `Growth` | the same gain in percent (Trending table only) |

**Trending this week** is the `+Stars` column sorted — a shortcut to the biggest
movers, not a separate ranking. Its repos also appear in the tables below it.

**By category** covers the long tail below the top leaderboard; repos already in the
leaderboard are not repeated there.

## Where the numbers come from

Membership is **not hand-curated**. `fetch.py` does an exhaustive sweep of **every**
public repo at or above the star floor (`min_stars` in `helpers/config.json`) into
`helpers/repos.json`, so discovery is complete by construction. `render.py` then
removes the two exclusion sets and publishes the top results by live star count.

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
- **Floor:** repositories under `min_stars`, and archived repositories, are excluded.
- **Star counts** are a point-in-time snapshot and drift daily.

## Where `Pos` and `+Stars` come from

Both are diffed against an older *committed* `helpers/repos.json` — the daily refresh
commits a full snapshot, so this repo's git history is a daily star series. The
baseline is the newest snapshot at least `render.trend.window_days` old, and both
columns use that same one, so they always describe the same span.

Two consequences worth knowing:

- `Pos` is computed by re-ranking the historical snapshot with **today's** exclusion
  sets, so it tracks real star movement rather than jumping whenever `filtered.json`
  or `scope_filter` changes who is eligible.
- The baseline is anchored to `repos.json`'s own `generated_at`, never to wall-clock
  time — re-rendering an old commit reproduces its original numbers.

No extra API calls are involved. A clone without git history (a depth-1 checkout)
simply renders without those columns; see `helpers/trend.py`.
