# Contributing

[`README.md`](README.md) and everything under `helpers/*.json` (except
`filtered.json`) are **generated** — don't edit them by hand; the daily workflow
overwrites them. To change what appears:

- **Re-classify what counts as in-ecosystem** — adjust `scope_filter` /
  `category_rules` in `helpers/config.json`. These are applied during the sweep, so
  they take effect on the next daily run (or a manual `refresh-ranking`
  `workflow_dispatch`).
- **Drop an AI-adjacent but redundant repo** — add a
  `{ "repo_id": "owner/name", "reason": "..." }` entry to `helpers/filtered.json`.
  This is the only editorial lever, and it re-renders immediately.
- **Change the layout, columns, or momentum window** — `helpers/render.py` and the
  `render.*` keys in `helpers/config.json`.

There is **no allowlist** and no hand-edited ranking: discovery is an exhaustive
sweep of every public repo above the star floor. See
[`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) for how the pipeline works, and
`.claude/skills/maintain-ranking-scripts/` for the full file contracts.

The scripts are **stdlib-only** — no `requirements.txt`, no third-party packages.
