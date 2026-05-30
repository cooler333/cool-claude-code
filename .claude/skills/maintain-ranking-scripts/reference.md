# maintain-ranking-scripts — reference

Extended reference for the ranking pipeline: file contracts, the `repos.json`
schema, config keys, failure modes, and rationale. The top-level `SKILL.md` is the
quick map; this is the detail.

## Pipeline data flow

```
config.json ─► fetch.py ─► repos.json ────────────┐
(knobs)        (network)   out_of_scope.json (auto)│
                                                   ├─► render.py ─► repos_to_render.json
                           filtered.json ──────────┘   (offline)   README.md
                           (manual editorial)
```

- **`fetch.py`** is the only networked component. It sweeps the full ≥ `min_stars`
  universe, writes `repos.json`, classifies scope-fails into `out_of_scope.json`,
  then stops. No Markdown, no categorization, no top-N.
- **`render.py`** is pure CPU: it reads `repos.json` + the two exclusion sets,
  selects the published top-N into `repos_to_render.json`, and writes `README.md`.
  Re-runnable anytime, deterministic, no network.
- **`config.json`** holds every knob. Changing scope/categories/count is a config
  edit, not a code edit.

## The four data files

| file | writer | role |
|------|--------|------|
| `repos.json` | `fetch.py` (daily) | the full universe: every repo ≥ `min_stars`, metadata only |
| `out_of_scope.json` | `fetch.py` (daily) | AUTO: universe entries that fail `scope_filter` (not AI/Claude) |
| `filtered.json` | human | MANUAL: AI-adjacent but redundant editorial exclusions |
| `repos_to_render.json` | `render.py` | DERIVED: the published top-N (universe − out_of_scope − filtered − archived) |

Universe partition: `repos.json` ⊇ (`out_of_scope.json` ∪ `filtered.json`).
**Single source of truth for "in scope" is `config.json`'s `scope_filter`** —
`out_of_scope.json` is a materialized cache of it, regenerated every run.

## repos.json schema (`schema_version: 3`)

Top-level keys: `generated_at`, `generator`, `schema_version`, `min_stars`,
`count`, `repos[]`. Entries are stars-sorted (desc).

| field | type | notes |
|-------|------|-------|
| `full_name` | str | `owner/name` |
| `stars` | int | stargazers at sweep time |
| `description` | str | upstream description (may be empty) |
| `topics` | list | GitHub topics |
| `archived` | bool | kept in the universe; excluded at render |

(No `rank`/`url`/`sources`/`redirected_from`/`readme_raw` — render derives `url`
from `full_name` and assigns display rank within the published set.)

`out_of_scope.json`: `{ description, generated_at, min_stars, count, out_of_scope:
[{ repo_id, stars, description, tags }] }`.
`filtered.json`: `{ description, filtered: [{ repo_id, reason, ... }] }` (only
`repo_id` affects filtering; a bare string is tolerated).
`repos_to_render.json`: `{ generated_at, generator, count, repos: [{ full_name,
stars, description, topics }] }`.

## config.json keys

| key | meaning |
|-----|---------|
| `top_table_size` | size of the leaderboard table in the README |
| `min_stars` | floor for inclusion (sweep lower bound) |
| `fetch.floor_fraction` | min sweep coverage (swept / live total) before fetch will overwrite |
| `render.render_count` | size of the published set written to `repos_to_render.json` |
| `render.category_order` | ordered category headings in the README |
| `render.other_category_label` | label for the catch-all bucket |
| `render.other_max` | warn threshold for the catch-all size |
| `brief.max_chars` | truncation for the description brief |
| `scope_filter` | relevance classifier (`require_match` + `any` terms) → drives `out_of_scope.json` |
| `category_rules` | `topic_map` + `keyword_rules` + `default_category` |
| `http.user_agent` | UA string for API calls |

## Failure modes & guarantees

- **Rate limits.** `ghclient.py` paces each API class and backs off on 403/429 +
  `Retry-After`. A run that still exhausts retries raises and the workflow fails
  **without** overwriting `repos.json` (stale data beats wrong data).
- **Low coverage.** If the sweep returns < `floor_fraction` of the live ≥
  `min_stars` total, `fetch.py` aborts rather than publish a half-empty universe.
- **Min-repo floor.** `render.py` refuses to write a README if the *published set*
  drops below its floor (guards a bad `filtered.json` / scope regression).
- **Determinism.** Same inputs → identical outputs (modulo `generated_at`).
  `render.py` is a pure function of its inputs.
- **Atomic writes.** Every file is written to a temp path and `os.replace`d, so a
  crash never leaves a partial file.
- **No conflicts by construction.** Each big file has one writer (the daily job);
  both workflows share the `refresh-ranking` concurrency group, and the commit step
  rebases before push to absorb any race.

## Rationale notes

- **Why an exhaustive sweep?** Scoped queries can silently miss a popular ecosystem
  repo. Sweeping every repo ≥ `min_stars` makes coverage complete by construction;
  classification (not discovery) becomes the only thing to tune.
- **Why no allowlist?** A hand-maintained include list rots and biases. Keeping the
  universe algorithmic means the ranking reflects real ecosystem signal.
- **Why two exclusion files?** They're orthogonal axes: topical relevance (auto,
  rule-driven) vs editorial redundancy (manual). Separating them keeps CI and humans
  off each other's files — no merge conflicts.
- **Why keep filtered repos in `repos.json`?** So the full set stays locally
  analyzable; exclusion happens only at render.
- **Why split fetch/render?** Network and formatting have different failure modes
  and cadences. Re-rendering after an editorial edit shouldn't require re-sweeping.
