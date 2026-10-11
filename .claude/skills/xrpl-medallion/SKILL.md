---
name: xrpl-medallion
description: Build, extend, run and check the XRPL silver and gold tiers (pipeline/run.py) on top of the bronze Parquet dataset. Use when adding or changing a silver or gold table, fixing a parse rule, running the incremental update or a rebuild, checking tier integrity, or wiring dashboards to gold.
license: CC-BY-4.0
compatibility: Designed for Claude Code on a host that also runs a live XRPL validator. Needs uv and the project venv; reads /local/data/xrpl.
metadata:
  project: xrpl-open-data-project
  version: "1.0"
---

# XRPL silver and gold tiers

Bronze (`/local/data/xrpl/{ledgers,transactions}/<first>-<last>.parquet`) is the raw, hash-verified,
published dataset and is **never written** by the pipeline. `pipeline/run.py` turns each bronze file
into silver (one row per event, `meta_json` parsed once) and recomputes small gold tables only for
the days and months a run touched. Design docs: the PRD doc (tabs: PRD, Epics and stories,
Architecture, Technical spec).

## Safety (this host is a live mainnet validator)

- DuckDB is capped at 2 threads and 4 GB, temp files under `/local/data/xrpl/pipeline/tmp`
  (`run.py: THREADS, MEMORY`). Do not raise these.
- Run long jobs as `nice -n 10 ionice -c3 uv run pipeline/run.py ...`.
- Never query the local xrpld in bulk. The pipeline makes one `server_info` call, in `status`.
- Never put data or temp files on `/` (98 GB, mostly full).
- No `apt install`. Python packages go through `uv add`.

## Commands

```bash
uv run pipeline/run.py update                       # new/changed bronze chunks -> silver -> gold
uv run pipeline/run.py update --months 2026-08 --tables transactions_base   # a slice
uv run pipeline/run.py rebuild --from-month 2026-03  # after a parse-rule change (one long run)
uv run pipeline/run.py invalidate --tables nft_events   # drop silver; then redo in batches:
uv run pipeline/run.py update --tables nft_events --gold nft_daily --limit 800
uv run pipeline/run.py regold --gold G --months 2026-01,2026-02   # gold only, from existing silver
uv run pipeline/run.py check                        # OK/FAIL lines; exit 1 on FAIL
uv run pipeline/run.py compact                      # merge closed months into all.parquet
uv run pipeline/run.py status                       # coverage per table, lag vs the validator
uv run pytest pipeline/tests -q                     # parse-rule fixtures on real transactions
ops/forward_export.sh                               # cron: export new ledgers, then update
uv run report/build_dashboard.py --from-gold        # dashboard from gold (seconds)
```

Every command takes `--data DIR` (default `/local/data/xrpl`, the real dataset). To test safely,
symlink a few bronze files into a scratch dir's `transactions/` and `ledgers/` and pass `--data`.
run.py finds `sql/` and `tiers.py` next to itself, so a copied `pipeline/` dir runs as-is
(`cd` to the project first so uv uses its venv).

## Layout

| Path | What |
|---|---|
| `pipeline/tiers.py` | Registry: silver order and flags (`nodes`, `pool_deltas`); gold kind (daily, monthly, running), reads |
| `pipeline/sql/macros.sql` | Amount helpers (`amt_asset`, `amt_value`, `amt_drops`, `issue_key`) and `xrpl_currency_code` |
| `pipeline/sql/silver/_nodes.sql` | Shared: `meta_json.AffectedNodes` exploded, one row per changed ledger object |
| `pipeline/sql/silver/_pool_deltas.sql` | Shared: AMM pool balance changes from the pool's side |
| `pipeline/sql/silver/<table>.sql` | One SELECT per silver table over `src` (one bronze file), `nodes`, `pool_deltas` |
| `pipeline/sql/gold/<table>.sql` | One SELECT per gold table over `s_<silver table>` views, `lg_src`, `{day_from}`, `{day_to}` |
| `pipeline/sql/gold/<table>.merge.sql` | Running tables only: merge `cand` into `g_existing` |
| `pipeline/checks.py` | Cross-tier checks and the dashboard regression values |
| `pipeline/tests/` | `fixtures.json` (real tx hashes + expected rows), `test_parse.py`, `test_codec.py` |
| `/local/data/xrpl/silver/<table>/month=YYYY-MM/<chunk>.parquet` | Silver; `all.parquet` after compaction |
| `/local/data/xrpl/gold/<table>/day=YYYY-MM-DD.parquet`, `month=YYYY-MM.parquet`, `<table>.parquet` | Gold |
| `/local/data/xrpl/pipeline/state.duckdb` | `bronze_chunks`, `silver_chunks`, `gold_parts`, `runs` |

## Add a silver table

1. Write `pipeline/sql/silver/<name>.sql`: one SELECT over `src`, and over `nodes` / `pool_deltas` if needed.
   Keep `ledger_index`, `close_time`, `hash` (and `tx_index` where order matters) on every row.
2. Register it in `tiers.SILVER` with `nodes=True` / `pool_deltas=True` as needed.
3. Find real transactions for each rule in a recent chunk (query `nodes` by `entry_type`/`tx_type`),
   check the expected values by reading the raw JSON yourself, and add them to `tests/fixtures.json`.
   Add a chunk-level invariant to `test_chunk_invariants` when one exists (e.g. a count that must match `src`).
4. `uv run pytest pipeline/tests -q`, then a scratch run, then `run.py update --tables <name>
   --limit N` in batches (it backfills only that table). If only some transaction types can produce
   its rows, set `tx_filter` in `tiers.SILVER` (see nft_events) and add a test that the fast path
   gives identical rows; re-parses then skip the expensive full `meta_json` explode.

## Add a gold table

1. Pick the grain: `daily` (`day` column, one file per day), `monthly` (`month` column), or `running`
   (whole-history: `<name>.sql` gives candidates from new chunks, `<name>.merge.sql` merges them; the
   merge must be order-independent, e.g. min/max/arg_max with a tie-breaker).
2. Write `pipeline/sql/gold/<name>.sql`. Output contract: a daily table must have a column named
   exactly `day` of type DATE (run.py splits files with `WHERE day = DATE '...'`); a monthly table a
   DATE column `month` (first of month). No ordering or primary key is required.
   - Inputs: `s_<silver table>` views (already filtered to the days being computed) and `lg_src`
     (bronze ledgers, with one extra day each side for lag/lead). `{day_from}` / `{day_to}` (DATE
     literals) are only needed when filtering `lg_src`, or anything else not pre-filtered.
   - Deterministic: order ties by `(ledger_index, tx_index)`; sum floats as `sum(x ORDER BY x)`.
   - Integers: `sum()` of BIGINT returns HUGEINT, which Parquet stores as DOUBLE. Cast drop and count
     sums back with `::BIGINT` (drops are int64 everywhere).
3. Register it in `tiers.GOLD` with its `reads` and `ledgers=True` if it uses `lg_src`.
4. **Backfill it with `run.py regold --gold <name>`.** `update` only recomputes days touched by new
   chunks, so on existing silver a new gold table is never built by `update`. A daily/monthly regold
   over the full year takes minutes per month: split it with `--months 2026-01,2026-02` to stay under
   10 minutes per call. Running tables are rebuilt from every chunk each regold, so regold them alone.
5. Prove determinism on scratch data whose chunks span **two months** (so `--from-month` splits it):
   fresh `update` on a new root holding only bronze symlinks, `regold`, full `rebuild`, and
   `rebuild --from-month <second month>` must give identical gold. Compare per table with
   `SELECT count(*), sum(hash(columns(*))) FROM read_parquet('<root>/gold/<name>/*.parquet')`
   (one value per column; compare the whole row).
6. Add what readers need: a check in `checks.py` if the table restates a number that can be
   cross-checked against bronze (e.g. per-day sums equal `network_daily`); the dashboard
   (`report/dashboard_gold.py`) if it feeds a view; the table list in AGENTS.md.

## Parse rules worth knowing (verified on real data)

- Offer fills are Offer nodes **with** `PreviousFields.TakerGets`; a deleted offer without it is a
  cancel or expiry. Fill = previous minus final.
- AMM swaps show as balance changes on the pool's AccountRoot (`AMMID` set) and on its trust lines;
  LP tokens (currency `03...`) are excluded from swap legs.
- Our `meta_json` has **no `nftoken_id`**: mints and burns come from the before/after diff of all
  NFTokenPages a transaction touched (one page's diff miscounts page splits).
- Payments: use the delivered amount, never `Amount` (partial payments, flag 131072).
- RippleState `Balance` is from the low account's side; positive means the low account holds it.
- Python UDFs cost ~0.3 ms per call: decode per distinct value (see `nft_events.sql`), or in SQL.

## Gotchas

- DuckDB `strftime` and `::DATE` follow the session time zone; `connect()` sets UTC.
- `state.duckdb` is locked while a run is going, so `status` (and any other reader) waits for the
  run's own lock and exits; follow progress in `/local/data/xrpl/pipeline/logs/run_YYYYMMDD.log`.
- First run registers every bronze chunk (two metadata reads per file, a few minutes for 8,000 files).
- `check`'s `dashboard_regression` compares whole months with the 2026-10-05 dashboard; p99 there
  used `approx_quantile`, hence the 5% tolerance.
- Measured: about 3.3 s per chunk for all silver tables (1.7-2.8 s of it is exploding `meta_json`),
  about 0.1 s for `transactions_base` alone. A full-year silver backfill is roughly 8 hours.
