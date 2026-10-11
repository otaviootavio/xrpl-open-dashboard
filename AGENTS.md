# AGENTS.md — XRPL Open Data Project

Open research dataset of XRP Ledger **mainnet** data, built on a machine that also runs a validator.
Human docs: `README.md`. Findings so far: `docs/spike-2026-09-28-xrpl-history.md`.

## Safety: the host also runs a validator

- This project runs on a machine that also runs an XRPL mainnet validator. Never restart, reconfigure or
  stop `xrpld` without the user's explicit go-ahead for that specific change.
- Package installs can restart services: warn before any `apt install` and suggest scheduling it.
- No bulk RPC to the local node; history comes from public full-history servers.
- DuckDB at 2 threads / 4 GB, long jobs under `nice -n 10 ionice -c3`, data and temp files off `/`.
- Host-specific facts (hostname, node version and status, disks, ports, incidents, private links) are in
  `AGENTS.local.md`, which is not in git.

## Layout

| Path | What |
|---|---|
| `xrpl_snapshot.sh` | Bash + jq snapshot of the last N ledgers from the local node → `data/`, `report/data.js` |
| `export_ledgers.py` | Resumable exporter: pool of public full-history servers → Parquet under `/local/data/xrpl`, with ledger hash checks |
| `make_datapackage.py` | Writes `/local/data/xrpl/datapackage.json` (Data Package v2) and `SHA256SUMS` |
| `report/build_export_report.py` + `report/export_template.html` | Builds the self-contained `report/export.html` |
| `report/index.html` | Report for `xrpl_snapshot.sh` (reads `report/data.js`) |
| `ops/backfill_watchdog.sh` | Cron, every 10 min: restarts the backfill if it died (not after `Done:` or a deliberate stop); logs to `/local/data/xrpl/watchdog.log` |
| `ops/build_report.sh` | Cron, hourly at :05: rebuilds `report/export.html`; logs to `/local/data/xrpl/report_build.log` |
| `ops/forward_export.sh` | Exports new ledgers after the newest on disk (public servers), then runs `pipeline/run.py update`. Meant for cron every 10 min; **not installed in crontab yet** |
| `pipeline/` | Silver and gold tiers: `run.py` (update, rebuild, check, compact, status), `tiers.py`, `sql/`, `checks.py`, `tests/`. How-to: `.claude/skills/xrpl-medallion/SKILL.md` |
| `report/build_dashboard.py` + `report/dashboard_template.html` | The 21-analysis dashboard (`report/dashboard.html`). `--from-gold` reads the gold tier via `report/dashboard_gold.py` (about 1 s); without it, it scans bronze (2.5 h) |
| `graph/` | Transfer graph stage: `run.py` (status, firstlook, entities, metrics, views, all, ego), `entities.py`, `metrics.py`, `hodge.py`, `tails.py`, `views.py`, `tests/`. How-to: `.claude/skills/xrpl-graph/SKILL.md` |
| `report/graph.html` + `report/graph/` + `report/vendor/` | Graph page (sigma.js 3.0.2, graphology 0.26.0, vendored with hashes in `vendor/VERSIONS`) drawing precomputed JSON views; `report/graph_proto.html` is the self-contained E2 prototype |
| `docs/` | Spike reports and write-ups |
| `.claude/skills/` | Agent Skills (open `SKILL.md` format) for repeatable procedures |
| `/local/data/xrpl/` | The dataset: `ledgers/`, `transactions/`, `runs/`, `datapackage.json`, `README.md`, `SHA256SUMS` |

## Commands

```bash
uv sync                                         # install deps (duckdb, pyarrow, httpx)
./xrpl_snapshot.sh 20                           # snapshot from the local node
uv run export_ledgers.py --hours 24             # export last 24 h (~22k ledgers, ~45 min)
uv run export_ledgers.py --start A --end B      # export a ledger range; rerun to resume
uv run export_ledgers.py --start A --end B --newest-first   # backfill backwards from B
uv run make_datapackage.py                      # refresh dataset metadata + checksums
uv run report/build_export_report.py            # rebuild report/export.html (reads the newest export_*.log)
uv run pipeline/run.py update                   # bronze -> silver -> gold, only what is new
uv run pipeline/run.py check                    # cross-tier OK/FAIL checks
uv run pytest pipeline/tests -q                 # parse-rule fixtures on real transactions
uv run graph/run.py all                         # transfer graph: entities -> metrics -> views (unchanged months skipped)
uv run graph/run.py status                      # graph months done, stale inputs
uv run pytest graph/tests -q                    # graph known-answer tests
```

### Silver and gold tiers (built 2026-10-09)

Design: the PRD doc "PRD: XRPL Medallion Data Tiers" (tabs PRD, Epics and stories, Architecture,
Technical spec) (link in AGENTS.local.md)

- Silver tables: transactions_base, transfers, dex_swaps, amm_events, nft_events, trustline_changes,
  account_lifecycle. Gold: network_daily/hourly/monthly, close_gaps_daily, dex_ohlcv_daily, amm_daily,
  token_daily, payment_size_daily, nft_daily, anomalies_daily, account_monthly, dest_tag_monthly,
  account_first_seen, records. `report/build_dashboard.py --from-gold` builds the dashboard from gold.
- **Status 2026-10-09:** gap closed (248,048 ledgers exported 05:48 UTC, then caught up to ~107,540,000);
  full-year silver for all 7 tables and all gold built (silver 71 GB, gold ~340 MB); `run.py check` all OK
  over 378 days; first full `verify_export.py` pass (log `/local/data/xrpl/verify_20261009.log`);
  `datapackage.json` + `SHA256SUMS` rebuilt for 8,394 chunks; dashboard rebuilt from gold (13 s) and republished.
- Verified: 21 tests pass (parse fixtures incl. brokered NFT sale and multi-hop payment; NFT burn spike
  13,895 burns = NFTokenBurn txs; NFT fast path identical); gold byte-identical across update, regold, rebuilds.
- Learned: a `tecINSUFF_FEE` tx burns less than its declared fee; the old dashboard's percentiles were
  approx_quantile (p90 off by 1 drop, p99 far off); most NFT sales are brokered (148,817 in the year): the buyer
  is the buy offer's owner, not the broker. A global `count(DISTINCT hash)` OOMs under the 4 GB cap, so
  verify_export now checks per chunk (2 workers, ~8 min). `make_datapackage.py` caches hashes
  (`pipeline/sha256_cache.json`) and takes `--max-seconds` to split the first hashing.
- Commands added: `run.py regold` (gold only), `run.py invalidate --tables T` then `update --tables T --limit N`
  (batched re-parse). Silver tables can declare `tx_filter` (nft_events: NFToken* only) for fast re-parses.
- Status of the node and what is waiting on the user: see `AGENTS.local.md`.
- Dashboard: one tab per view (URL hash, e.g. `#dex`) and a freshness pill / callout that turns amber when the data
  is more than an hour behind the chain. A **session-only** Claude Code cron (minute 17, expires after 7 days or
  when the session ends) exports new ledgers, runs `pipeline/run.py update`, rebuilds the dashboard from gold and
  republishes it; recreate it in a new session until the system crontab jobs exist.

### Transfer graph analysis (planned 2026-10-10)

PRD: "PRD: XRPL Transfer Graph Analysis" (tabs PRD, Epics and stories, Architecture, Technical spec with one sub-tab per epic E1–E6) (link in AGENTS.local.md)
Monthly account-to-account graph from silver `transfers` (gold `transfer_edges_monthly`), entity grouping,
graph metrics (PageRank, bow-tie, Leiden, cycles, Hodge loop share), sigma.js Graph tab. Sept 2026 measured:
18.9 M transfers, 390 k accounts, 1.74 M directed pairs. Not started; M1 (edge table + Sept look) is next.
Measured 2026-10-10: the E1 edge SQL on Sept 2026 takes 24.5 s, gives 1,784,108 edges (107 MB Parquet), and its
n_transfers sum equals the 18,929,073 non-self transfers. Only 56% of Sept accounts were created in our window
(have `funded_by`); the top funders created 34–79k accounts each (exchanges activating customers), so "same funder"
must never mean "same entity" on its own. Graph state goes in `pipeline/graph_state.duckdb` (DuckDB is single-writer;
run.py holds state.duckdb). No Node.js on the host: vendor prebuilt sigma.js/graphology files.

### 12-month backfill — **finished** 2026-10-01 12:38 UTC

Started 2026-09-28 02:28 UTC (restarted 04:10 UTC with the dispatcher), command for reference:

```bash
cd ~/xrpl-open-data-project && setsid nohup nice -n 10 ionice -c3 uv run export_ledgers.py \
  --start 99147000 --end 107259774 --newest-first >> /local/data/xrpl/export_backfill.log 2>&1 < /dev/null &
```

- Result: 8,136 chunks, ledgers 99,147,000–107,281,933 (7,664,000 ledgers), 788,813,053 transactions, 342 GB.
  Exporter is idle; the cron watchdog (`ops/backfill_watchdog.sh`) correctly leaves it stopped after `Done:`.
- Done 2026-10-09: full `verify_export.py` pass and `make_datapackage.py` (see "Silver and gold tiers" above).
- Gap closed 2026-10-09 (see above); it grows again until `ops/forward_export.sh` runs from cron.
- To extend the dataset forward, rerun `export_ledgers.py` with `--start 107281934 --end <new tip>` (not
  `--newest-first`), 1,000-aligned, against the public endpoints — not the local validator.

## Git

The project folder is a git repo: branch `main`, public remote `git@github.com:otaviootavio/xrpl-open-dashboard.git`.
Keep host-specific facts and private links out of tracked files: they go in `AGENTS.local.md` (ignored).
Generated outputs are ignored (`.gitignore`): `report/graph/`, rebuilt reports, `data/` snapshots.
Commit code and docs; never the dataset (it lives in `/local/data/xrpl`). How to reproduce everything
from scratch is in README.md ("Quickstart", "Reproduce the whole dataset").

## Data conventions

- Parquet, zstd level 9, chunks aligned to 1,000 ledgers, named `<first>-<last>.parquet` (10-digit, zero-padded).
  Chunks are written via `.tmp` + rename, so a file on disk is always complete.
- XRP amounts in **drops** (int64); issued-currency amounts as **strings** (precision); times **UTC**.
- `tx_json` / `meta_json` keep the full raw RPC output, so no field is lost.
- The exporter recomputes every ledger header hash (SHA-512Half over `LWR\0` + header fields) and rejects
  mismatches, checks the parent-hash chain inside each chunk, and links each chunk to its neighbours on disk
  (`stored_ledger()`); on a break it exits without writing. Transactions are not yet checked against
  each ledger's transaction root hash (`transaction_hash`).
- Integrity checks after any export: no gaps (`count(*) == max-min+1` on ledgers), no duplicate `hash`,
  `sum(tx_count) == count(transactions)`, and XRP burned (drop in `total_drops`) ≈ `sum(fee_drops)`.

## Gotchas learned the hard way

- **DuckDB uses the machine's local time zone (UTC−3)** for `strftime` on timestamptz. Always run
  `SET TimeZone = 'UTC'` first.
- DuckDB rejects `first`, `last`, `hour` as bare aliases: write `AS hour`, or rename them.
- When reading Parquet while an export is running, **freeze the file list first** and pass it to
  `read_parquet([...])`; a glob can pick up a new chunk mid-query and give inconsistent totals.
- Fetching is a dispatcher (the loop after "# Dispatcher:" in `main()`, state in `Endpoint`): each request is one task sent to the
  endpoint with the lowest expected wait; per-endpoint concurrency adapts between 1 and its cap in `ENDPOINTS`
  (+1 per `limit` successes, halved and paused on throttling); total in flight ≤ `MAX_IN_FLIGHT` (24);
  `WINDOW` = 2 chunks in flight so workers don't idle at chunk ends; a ledger stuck > 3× median latency on the
  oldest chunk gets a duplicate request (first answer wins). Ledgers are converted to rows on arrival (~0.8 GB RSS).
- Any per-request error (including odd ones such as Clio's `failedToForward`, which stopped the backfill on
  2026-09-28 08:11 UTC) counts against that endpoint and the ledger is retried elsewhere; a ledger failing
  `MAX_ATTEMPTS` (30) times stops the run. New error codes are logged once as `new error '...'`.
- Benchmarks (3,000 ledgers each, 2026-09-28): old s2 + xrplcluster 10.6 ledgers/s; fixed pool of 20 workers
  24.6/s (evening) and 31.3/s (04:00 UTC); dispatcher 30.7/s at 04:00 UTC on ledgers with ~40% more transactions. Ankr and honeycluster
  run at their caps with almost no errors. xrplcluster.com, xrpl.ws (same cluster) and xrpl.link throttle hard (`slowDown`, `tooBusy`).
  Tatum serves only recent history; Uniblock and dRPC gave no usable answer.
- `binary: true` was measured and **not adopted**: responses ~33% smaller but no faster on s2
  (server time dominates), and it would need xrpl-py, recomputed `delivered_amount`/`hash`, and a
  different `tx_json`. Its real upside, verifying each ledger's transaction root hash, is a future item.
- The report's goal block (title, 12-month window, milestones) comes from `GOAL` in `report/build_export_report.py`;
  update it when a milestone changes. Design tokens: teal accent, IBM Plex Sans / Condensed / Mono.
- The report scopes transaction stats to the most recent ~22,500 ledgers on disk; whole-dataset totals
  come from `ledgers/` only. DuckDB is capped at 2 threads / 4 GB to protect the validator.
- `frictionless validate` on Parquet needs `uvx --from 'frictionless[parquet]' --with pandas`; pandas
  turns null integers into `nan` (hence `missingValues: ["", "nan"]`), and it miscounts `bytes` on
  multipart resources (so the descriptor omits `bytes`).
- System Python has no pip/venv (`python3.12-venv` missing). Use uv for everything.
- The `!` prefix only works in the Claude Code prompt, not in the user's own terminal.
- Background jobs live in the user crontab (`crontab -l`): report build at :05 and backfill watchdog every 10 min.
  They run without Claude. Only **publishing** needs Claude: a session-only Claude Code cron at :09 republishes
  `report/export.html` if it is fresh. It dies with the session and expires after 7 days, so recreate it in a new session.
  The page shows "Updated … ago" and turns amber after 75 min, so a missed publish is visible.

## Storage numbers for planning

| Store | Size |
|---|---|
| Our Parquet | ~61 KB per ledger (~450 bytes per transaction), ~1.3 GB per day |
| xrpld NuDB (xrpl.org) | ~450 KB per ledger: 12 GB/day, 4.5 TB/year, ~39 TB full history (Jan 2026) |
| Clio (Aug 2023) | ~1.9 TB for full history; no backfill, ingests forward from an xrpld that holds the ledgers |
| Full chain estimate | ~107M ledgers, ~5.3B transactions, ~2.3–2.8 TB as our Parquet |

## Decisions and open questions

- Store history as **Parquet on the HDD array**, not as a node database (xrpld needs SSD; the HDDs are ~10× too slow).
- Don't run a history node, Clio or Cassandra next to the validator without the user's decision (RAM and isolation).
- Next steps: 12-month backfill **finished** 2026-10-01 (see Commands); `verify_export.py` + `make_datapackage.py`
  still need to be rerun against the full dataset. Not started: forward export from the local node or public
  endpoints every 10 min (gap after 107,281,933 is ~158,000 ledgers and growing); raise `online_delete` to
  ~200,000 in one planned restart; dedicated history node on separate hardware; ask the XRPL Foundation /
  Ripple UBRI for bulk access before any full-chain export (~107M ledgers ≈ 2 months at 24/s).
- Open: dataset licence (CC BY 4.0 placeholder vs CC0) and contributor names in `make_datapackage.py`.
