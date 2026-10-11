---
name: xrpl-graph
description: Build, run, extend and check the XRPL transfer graph (who pays whom): the gold edge table, entity grouping, graph metrics (PageRank, bow-tie, Flow Index, Leiden communities, cycles, Hodge loop share) and the sigma.js graph page. Use when asked about account-to-account flows, hubs, exchanges, communities, loops or wash trading, when adding a graph metric or view, or when the graph tables need a rebuild.
license: CC-BY-4.0
compatibility: Designed for Claude Code on a host that also runs a live XRPL validator. Needs uv and the project venv; reads /local/data/xrpl gold and silver.
metadata:
  project: xrpl-open-data-project
  version: "1.0"
---

# XRPL transfer graph

Design docs: the doc "PRD: XRPL Transfer Graph Analysis" (tabs PRD, Epics and stories, Architecture,
Technical spec with E1–E6), linked from AGENTS.md.

```
silver transfers ──(pipeline/run.py, SQL)──> gold transfer_edges_monthly
                                                 │
                       graph/run.py entities ────┤  + silver account_lifecycle, gold dest_tag_monthly
                         gold graph_entities, graph_entity_edges
                       graph/run.py metrics  ──> gold graph_nodes, graph_communities, graph_cycles, graph_summary
                       graph/run.py views    ──> report/graph/*.json  (drawn by report/graph.html)
                         uses graph/layout.py (E7), graph/backbone.py (E8), graph/names.py (E9)
```

Readability design (E7-E10): the doc "PRD: XRPL Graph Readability" (tabs PRD, Architecture, Epics and stories,
Technical spec), linked from AGENTS.local.md; spike results in `docs/graph-layout-spike.md`.
**No AI in the product**: names, summaries and layouts are computed from the data. AI (Claude) is a development
tool here: write the code, then render previews and look at them (below).

## Safety (live mainnet validator)

- DuckDB at 2 threads / 4 GB (`common.connect`); BLAS single-threaded (set in `common.py` before numpy loads).
- Long jobs: `nice -n 10 ionice -c3 uv run graph/run.py ...`. `common.check_rss` fails a step above 6 GB RSS.
- No RPC to the local node; data and temp files under `/local/data/xrpl`; packages only via `uv add`.
- Graph state is `pipeline/graph_state.duckdb`, never `state.duckdb` (DuckDB allows one writer per file and
  `pipeline/run.py` holds that one while it runs).

## Commands

```bash
uv run graph/run.py status                          # months done per step, stale inputs
uv run graph/run.py all --months 2026-08,2026-09    # entities -> metrics -> views; unchanged months skipped
uv run graph/run.py metrics --months 2026-09 --force
uv run graph/run.py firstlook --month 2026-09       # account-level hubs/tails -> docs/graph-first-look-*-tables.md
uv run graph/run.py ego rADDRESS --month 2026-09 --asset XRP   # one ego view on demand
uv run pipeline/run.py regold --gold transfer_edges_monthly --months 2026-09   # rebuild edges for a month
uv run pytest pipeline/tests/test_edges.py graph/tests -q
uv run graph/preview.py report/graph/backbone_2026-09_XRP.json /tmp/bb.png     # PNG at the reference stage + overlap/label counts
uv run graph/preview.py report/graph/community_detail_2026-09_XRP.json /tmp/c.png --community C0
uv run ops/stage_graph_site.py --out <scratch dir>   # published copy + publish_plan.json (artifact limits checked)
```

## Readable views (E7-E10)

- **Layout** (`layout.place`): ForceAtlas2 (LinLog) from the previous month's positions, then `remove_overlaps`
  in the normalised frame, where 1 unit = `PX_PER_UNIT` (320) px on the 1,000 x 700 reference stage. Zero
  overlaps at default zoom is the target and is logged per view. Sizes 3 to 18 px (`node_sizes`, sqrt scale).
- **Labels**: nodes carry `label_rank`; the page shows rank < 30 at default zoom and more as you zoom in.
  Entity labels are the first 8 characters of the entity id (the role is the colour).
- **Edges** (`backbone.keep_mask`): disparity filter (alpha 0.05, stepping down to fit the cap) + each node's
  strongest edge + a maximum spanning forest, so views stay connected. Caps: backbone 1,000, ego 200,
  community bundle 400. Kept edges have `bb = 1`; the layout uses only those; "Show all edges" draws the rest.
  Edges are drawn as animated dots (canvas overlay in `graph.html`, no data change): speed = log of mean transfer size (`value/n`),
  dot count = log of transfer count `n`. It encodes volume, not timing. "Animate flow" off (or reduced motion) falls back to arrowheads.
  Sample hashes are looked up for `bb` edges only (the lookup scans a month of silver transfers).
- **Names** (`names.py`): role word of the community's top-PageRank member + its 8-char id, e.g.
  "Exchange hub · rLHzPsX6"; summary = size, share kept inside, largest outflow.
- **Bundles**: `community_detail_<month>_<label>.json` holds the 10 largest communities, each a group of up to
  150 entities grown from its top entity along the strongest ties (`views.grow`).
- **Versioning**: view-only changes bump `VIEWS_VERSION` in `common.py` (rebuilds views, skips entities and
  metrics); `GRAPH_VERSION` rebuilds everything.
- Page URL hash: `#<view>~<month>~<label>[~<arg>][~all]` (arg = `C<n>` or an entity id); Back works.

`ops/forward_export.sh` runs `graph/run.py all` for the open month once an hour (minute < 10).

## Layout

| Path | What |
|---|---|
| `pipeline/sql/gold/transfer_edges_monthly.sql` | Edges: one row per (month, from, to, asset); check `edges_match_transfers` in `pipeline/checks.py` |
| `graph/common.py` | Paths, connection, `GRAPH_VERSION`, `SEED`, assets analysed, atomic writes |
| `graph/load.py` | A month of edges (account or entity level, one asset or `*`) as sorted integer arrays / igraph |
| `graph/entities.py` | E3 rules (issuer, funding), union-find, size cap and hub guard, entity edges |
| `graph/metrics.py` | E4 metrics per asset; `graph/hodge.py` (net flow, CG on the Laplacian), `graph/tails.py` (power-law fits) |
| `graph/views.py` | E5 view JSON (backbone, communities, community bundles, ego, loops, index) |
| `graph/layout.py`, `graph/backbone.py`, `graph/names.py` | E7 layout + overlap removal, E8 edge filter, E9 community names |
| `graph/preview.py` | Development aid: render a view to PNG and count overlaps / label collisions |
| `ops/stage_graph_site.py` | Stage the published copy and plan the artifact publishes |
| `graph/firstlook.py` | E2 account-level first look + `report/graph_proto.html` |
| `report/graph.html` + `report/vendor/` | The page (sigma.js 3.0.2 + graphology 0.26.0, versions and hashes in `vendor/VERSIONS`) |

## Add a metric

1. Compute it in `metrics.asset_metrics` from `mg` (arrays) or `g` (igraph); keep vertex order (ids are
   sorted entity ids) and break ties by vertex id.
2. Add the column to `NODE_COLS`/`NODE_TYPES` or the `summary` dict; round floats with `round_sig`.
3. Bump `GRAPH_VERSION` in `common.py` (every month then recomputes), add a known-answer test to
   `graph/tests/test_graph.py`, run it, then `graph/run.py metrics --force` for one month and check the timing.
4. Determinism: a second `--force` run must give the same `SELECT count(*), sum(hash(x)) FROM read_parquet(...) x`.
5. Mention it in the Technical spec tab (E4) and in AGENTS.md if readers need it.

## Entity rules

Thresholds are constants at the top of `graph/entities.py`. Same funder is **not** same owner: exchanges
activate tens of thousands of customer accounts, so the funding rule only links children of small funders
(<= 1,000 accounts) whose transfers stay >= 80% inside the tree. Any threshold change needs a new review
note (`docs/graph-entity-review-<month>.md`) before results are published.

## Gotchas

- An ordered float sum (`sum(x ORDER BY x)`, needed for byte-identical output) next to other aggregates in
  one GROUP BY ran out of memory under the 4 GB cap on months with a 1.4 M-transfer pair: compute it in its
  own aggregation and join (see the edge SQL and `entities.py`).
- MPT payments have no currency/issuer in silver, so `asset` is NULL there; edges keep them as `MPT`.
- igraph's `community_leiden` (11 s, modularity 0.63 on a month of XRP) instead of leidenalg (108 s, 0.52).
- Hodge: `lsqr` on the incidence matrix took 153 s per month and asset; Jacobi-preconditioned CG on the
  Laplacian takes about 1 s with the same answer. The potential is centred per component (L is singular).
- powerlaw's own x_min search took more than 5 minutes on a month: `tails.py` picks x_min on a 40-quantile grid.
- `pkill -f "graph/run.py ..."` also matches the shell running it: use `pgrep` first, then kill by PID.
- Flow Index (arXiv 2106.10012 eq. 13): the paper's worked example (A = 1.0005, …) does not match its own
  formula (it gives about 1.00005); we follow the formula, tested against the paper's M_n test family.
