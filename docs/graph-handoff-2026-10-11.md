# Graph tab handoff, 2026-10-11

What changed in the Graph tab since the first version, how to rebuild it from nothing, and what has not been checked.

## What was built

**Pipeline (Python, tested, reproducible).** The views are readable now:
- `graph/layout.py`: ForceAtlas2 (LinLog) in numpy, then overlap removal. 2026-09 XRP backbone went from 1,414 overlapping node pairs to 0.
- `graph/backbone.py`: disparity filter (Serrano et al.), plus each node's strongest edge and a maximum spanning forest. About 500 of 3,200 edges are drawn.
- `graph/names.py`: rule-based community names (role word + first 8 characters of the top member). No AI anywhere in the product.
- `graph/views.py`: backbone, community map, community bundles (`community_detail_*.json`), ego views. `VIEWS_VERSION = "2"`: a view-only change skips the entity and metric steps.
- `graph/common.py`: `GRAPH_RSS_LIMIT_MB` env var lifts the 6144 MB RSS guard for one run (2026-08 is about 4x bigger than other months and peaks at about 5.5 GB).
- `graph/preview.py` renders a view to PNG and prints overlap and label-collision counts. `graph/tests/`: 27 known-answer tests.

**Page (`report/graph.html`, one file, no build step).**
- One model: every edge is a *connection*. `view.rules` decide which connections are in view (min total value moved, min total payments, backbone or all, focused node). `styles.trace` (line) and `styles.flow` (moving dots) decide how they look. Add a rule or a style there; never special-case one in the other. `restyle()` applies a change.
- Flow dots: dot size = log of mean payment size (`value / n`), how often dots leave the payer = payment count `n` (every 3 s for the busiest connections, every 60 s for the quietest, so quiet ones sleep), speed constant (25 px/s, in-out quad ease, at most 5 dots per line). It shows relative activity, not timing.
- Click a node to lock the focus; click it again, the background or Esc releases it. On the community map a double click drills down.
- Month is a slider. On the backbone view a month change animates (`morph()`, `Sigma.utils.animateNodes`, 700 ms).
- "By period" view: bars by month or by year for one measure (sums for transfers, averages of months otherwise).
- No all-assets view in the page (amounts of different tokens do not add up). The pipeline still builds it; `ops/stage_graph_site.py` leaves it out of the published copy.
- Ego view: falls back to the latest month that has ego views and says so.

## Rebuild from nothing

```bash
cd ~/xrpl-open-data-project
uv sync
uv run pytest graph/tests -q                          # 27 passed
uv run graph/run.py status                            # months done, stale inputs
nice -n 10 ionice -c3 uv run graph/run.py all         # entities -> metrics -> views; unchanged months are skipped
# 2026-08 only, if the RSS guard trips:
GRAPH_RSS_LIMIT_MB=7000 nice -n 10 ionice -c3 uv run graph/run.py all
```

Generated output (`report/graph/`) is git-ignored. In the last full views rebuild of 14 months each month took about 4 to 7 minutes and 2026-08 took 824 s (log `/local/data/xrpl/pipeline/logs/graph_readable_rebuild.log`). Constraints: DuckDB 2 threads / 4 GB, data off `/`, never touch the running xrpld, no bulk RPC.

To look at the page locally: serve `report/` (any static server) and open `graph.html`; it fetches `graph/*.json` and `vendor/*.js`.

## Publishing

The page is a private claude.ai artifact (link in `AGENTS.local.md`, not in git). The limits are 511 files and 256 MB per version, 255 files and 64 MB per publish.

```bash
nice -n 10 uv run ops/stage_graph_site.py --out /path/to/stage   # ego views for the latest full month; writes publish_plan.json
```

Then publish `stage/graph.html` to the same artifact URL with the Artifact tool; add the files of each batch in `publish_plan.json` when data changed. A page-only change needs only `graph.html`. If the data changed, also publish `graph/index.json` and the changed JSON. Files already on the artifact stay unless removed. The published copy is a snapshot: ego views for one month only, no all-assets files in the staged copy (older versions still hold unused ones).

## Not verified

- **No browser test of anything in the page.** The host has no browser and no Node. The JavaScript was syntax-checked with QuickJS (`uv run --with quickjs`) and the Sigma calls were checked against the vendored bundle. Click through: drill-down, breadcrumbs, Back, click-to-lock, double click on a community, sliders, the month animation, the By period view, dots (size, gaps, ease).
- **Node and ego views at another month** have not been looked at after the refactor.
- **Month animation** may look wild because each month's layout is computed separately (195 of 300 backbone nodes are shared between 2026-09 and 2026-10, and shared nodes can sit far apart). The fix is to seed each month's layout from the previous one in `graph/layout.py` and rebuild all 14 months (long job on the host; ask first).

## Open questions (from the PRD)

Edge weight for the filter: count or value? Is the 1,000-edge cap right per asset? Use public exchange address lists for names? Republish cadence?

## Where things are

| What | Where |
|---|---|
| How-to, view versions, design notes | `.claude/skills/xrpl-graph/SKILL.md` |
| Layout spike (why FA2) | `docs/graph-layout-spike.md` |
| PRD, architecture, epics, spec | Claude Doc "PRD: XRPL Graph Readability" (link in `AGENTS.local.md`) |
| Published page | artifact link in `AGENTS.local.md` |
