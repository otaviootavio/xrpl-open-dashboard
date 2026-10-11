# XRPL Open Data Project

An open research dataset of XRP Ledger **mainnet** history: every ledger and every transaction, with
its raw metadata, stored as Parquet files that anyone can query with DuckDB, pandas or R.
It is built on a machine that also runs one of our XRPL mainnet validators.

- Dataset: `/local/data/xrpl` (18 TB HDD array)
- Detailed notes for agents and maintainers: [AGENTS.md](AGENTS.md)
- Background research: [spike report, 27–28 Sep 2026](docs/spike-2026-09-28-xrpl-history.md)
- Design docs (PRDs, architecture, specs) are private claude.ai docs, linked from `AGENTS.local.md` on the host

## Quickstart

You need Linux, Python 3.12 and [uv](https://docs.astral.sh/uv/). No XRPL node is needed: ledgers come from
public full-history servers. Plan for ~1.3 GB of Parquet per day of mainnet history, on a disk that is not `/`.

```bash
git clone <this repo> xrpl-open-data-project && cd xrpl-open-data-project
uv sync                                           # duckdb, pyarrow, httpx, numpy, igraph, scipy, powerlaw
export DATA=/path/to/data                         # every tool defaults to /local/data/xrpl
uv run export_ledgers.py --hours 2 --out $DATA    # ~2,000 ledgers from public servers, a few minutes
uv run .claude/skills/xrpl-ledger-export/scripts/verify_export.py --data $DATA
uv run pipeline/run.py update --data $DATA        # bronze -> silver -> gold
uv run pipeline/run.py check --data $DATA         # OK/FAIL lines
XRPL_DATA=$DATA uv run graph/run.py all           # transfer graph: entities -> metrics -> views
cd report && python3 -m http.server 8000          # then open http://localhost:8000/graph.html
```

`graph.html` loads its views with `fetch`, so open it through a web server, not as a `file://` page.

## Reproduce the whole dataset

Every step is deterministic and resumable: a rerun skips what is already on disk, and the derived tiers
give byte-identical tables on a rebuild (checked with `SELECT count(*), sum(hash(x)) FROM read_parquet(...) x`).

| Step | Command | Time on this host (i5-9400F, HDD RAID1) |
|---|---|---|
| 1. Bronze: ledgers and transactions | `uv run export_ledgers.py --start 99147000 --end 107281933 --newest-first --out $DATA` | ~3.5 days at ~25–30 ledgers/s (342 GB) |
| 2. Verify bronze | `uv run .claude/skills/xrpl-ledger-export/scripts/verify_export.py --data $DATA` | ~8 min |
| 3. Package metadata | `uv run make_datapackage.py --data $DATA` (`--max-seconds` splits the first hashing) | ~1 h the first time |
| 4. Silver and gold | `uv run pipeline/run.py update --data $DATA` (or in batches with `--limit N`) | hours for a full year; then minutes per new day |
| 5. Check the tiers | `uv run pipeline/run.py check --data $DATA` | a few minutes |
| 6. Transfer graph | `XRPL_DATA=$DATA uv run graph/run.py all` | ~8 min per month |
| 7. Dashboard | `uv run report/build_dashboard.py --from-gold --data $DATA` | seconds |
| 8. Tests | `uv run pytest pipeline/tests graph/tests -q` | ~30 s (parse tests read two real bronze chunks) |

To keep it current, run `ops/forward_export.sh` from cron every 10 minutes: it exports new ledgers,
updates silver and gold, and once an hour recomputes the open month of the transfer graph.
On a machine that also runs a validator, keep the limits in [AGENTS.md](AGENTS.md): DuckDB at
2 threads / 4 GB, jobs under `nice -n 10 ionice -c3`, no bulk RPC to the local node.


## Status (last updated 11 Oct 2026)

| | |
|---|---|
| Dataset | Ledgers 99,147,000 → ~107,540,000 (12+ months from 28 Sep 2025): bronze verified (`verify_export.py`, 9 Oct) and packaged (`datapackage.json` + `SHA256SUMS` for 8,394 chunks) |
| Silver and gold | Built for the whole range on 9 Oct; `pipeline/run.py check` all OK |
| Transfer graph | Edge table for all 14 months (10 Oct); entities, metrics and views being computed month by month (`graph/run.py status`) |
| Not automatic yet | `ops/forward_export.sh` is not in crontab, so the dataset only grows when someone runs it |

## What is running

Everything below runs without anyone logged in, except publishing the status page.

| What | How | Log (in `/local/data/xrpl/`) |
|---|---|---|
| Backfill download | Detached `export_ledgers.py` process, low CPU/IO priority | `export_backfill.log` |
| Watchdog | User crontab, every 10 min: `ops/backfill_watchdog.sh` restarts the backfill if it died | `watchdog.log` |
| Report build | User crontab, hourly at :05: `ops/build_report.sh` rebuilds `report/export.html` | `report_build.log` |
| Report publish | A Claude Code session republishes the page at :09 each hour | – |

The watchdog does **not** restart the backfill after it finishes (`Done:` in the log) or after a
deliberate stop (a broken hash chain, or one ledger failing on every server). Those need a person to
read the log first.

Publishing needs a Claude Code session because the page is a claude.ai artifact. That job lives only
as long as the session and expires after 7 days. If it stops, downloads and builds carry on and only the
page goes stale. The page then shows "Updated … ago" in amber. To restore it, open Claude Code in this
folder and ask it to recreate the hourly publish job.

## Operating it

```bash
tail -f /local/data/xrpl/export_backfill.log        # progress, one line per 1,000 ledgers
pgrep -af 'python export_ledgers[.]py'              # is the exporter running? (this pattern avoids matching itself)
crontab -l                                          # the watchdog and report build jobs
cat /local/data/xrpl/watchdog.log                   # restarts, if any
```

To start the backfill by hand, first check that none is running (never run two exporters at once):

```bash
cd ~/xrpl-open-data-project && setsid nohup nice -n 10 ionice -c3 uv run export_ledgers.py \
  --start 99147000 --end 107259774 --newest-first >> /local/data/xrpl/export_backfill.log 2>&1 < /dev/null &
```

When the backfill finishes:

```bash
uv run .claude/skills/xrpl-ledger-export/scripts/verify_export.py   # gaps, duplicates, counts, burn vs fees
uv run make_datapackage.py                                          # datapackage.json + SHA256SUMS (~1 h on the HDDs)
```

**The machine is a live validator.** Never restart or reconfigure `xrpld` casually, and warn before any
`apt install` (a package install restarted it once). See the safety section in [AGENTS.md](AGENTS.md).

## Silver and gold tiers

Bronze (the raw Parquet export) is turned into two derived tiers by `pipeline/run.py`:

- **Silver**: one row per event, with `meta_json` parsed once: `transactions_base`, `transfers`,
  `dex_swaps` (order-book fills and AMM swaps), `amm_events`, `nft_events`, `trustline_changes`,
  `account_lifecycle`. Stored under `/local/data/xrpl/silver/<table>/month=YYYY-MM/`.
- **Gold**: small pre-computed tables per view (network, DEX, AMM, tokens, NFTs, accounts, anomalies,
  records) under `/local/data/xrpl/gold/`, recomputed only for the days new data touched.

```bash
uv run pipeline/run.py update      # process whatever is new
uv run pipeline/run.py check       # tiers agree with bronze (OK/FAIL)
uv run pipeline/run.py status      # coverage and lag
uv run pytest pipeline/tests -q    # parse rules on real transactions
```

Design and backlog: the medallion PRD (a private claude.ai doc).
Procedures for agents: `.claude/skills/xrpl-medallion/SKILL.md`.

## Transfer graph

Who pays whom, month by month. Built from silver `transfers` (Payment, CheckCash, EscrowFinish; delivered
amounts):

- **Edges** (gold `transfer_edges_monthly`): one row per (month, sender, receiver, asset). Its sum matches
  every non-self transfer in silver (`pipeline/run.py check`: `edges_match_transfers`).
- **Entities** (`graph_entities`, `graph_entity_edges`): accounts grouped only on on-ledger evidence, an
  issuer with its distribution wallet, or a small funding tree that transacts mostly within itself.
  Exchanges activate tens of thousands of customer accounts, so "same funder" alone never groups accounts.
- **Metrics** (`graph_nodes`, `graph_communities`, `graph_cycles`, `graph_summary`), per month for XRP,
  RLUSD, the next 8 tokens and all assets together: degrees, PageRank, bow-tie, Flow Index, Leiden
  communities, 2- and 3-cycles, Hodge loop share, power-law tails, churn.
- **Views**: `report/graph.html` draws precomputed JSON (backbone, community map, ego views, loops) with
  sigma.js; libraries are vendored in `report/vendor/` (versions and SHA-256 in `VERSIONS`).

```bash
uv run graph/run.py status                         # what is done, what is stale
uv run graph/run.py all --months 2026-09           # one month (unchanged months are skipped)
uv run graph/run.py ego rADDRESS --month 2026-09   # an ego view for any account
uv run graph/run.py firstlook --month 2026-09      # account-level hub tables -> docs/
```

Findings so far: [first look at September 2026](docs/graph-first-look-2026-09.md).
Procedures for agents: `.claude/skills/xrpl-graph/SKILL.md`.

## How the export works

`export_ledgers.py` fetches whole ledgers (`ledger` with `transactions` and `expand`) from public
full-history servers, not from our validator, which keeps only about the last hour of ledgers.

- **Servers:** Ripple s1 and s2, honeycluster.io, Ankr, xrplcluster.com and xrpl.link.
- **Dispatcher:** each request goes to the server with the lowest expected wait. Each server's
  concurrency adapts on its own: up after successes, halved and paused when it throttles us. At most 24
  requests are in flight. A ledger that holds up a chunk for too long is requested from a second
  server, and the first answer wins.
- **Verification:** every ledger header is re-hashed and must match its ledger hash. Each chunk must
  chain to its neighbours on disk through `parent_hash`. On any break the run stops without writing.
  Transactions are not yet checked against each ledger's transaction root hash.
- **Output:** 1,000-ledger chunks written atomically to `ledgers/` and `transactions/`, so a rerun
  resumes where it stopped. `--newest-first` walks backwards in time, so what is on disk is always
  one continuous range.

```bash
uv sync                                                # one-time: installs duckdb, pyarrow, httpx
uv run export_ledgers.py --hours 24                    # last 24 h
uv run export_ledgers.py --start A --end B             # a ledger range
uv run export_ledgers.py --start A --end B --newest-first
```

| Path under `/local/data/xrpl` | Contents |
|---|---|
| `ledgers/<first>-<last>.parquet` | One row per ledger: index, hashes, close time, total drops, tx count |
| `transactions/<first>-<last>.parquet` | One row per transaction: type, account, destination, fee, result, delivered amount, plus the raw `tx_json` and `meta_json` |
| `runs/<ts>.json` | Range, endpoints and per-endpoint counts for each run |

Query with DuckDB, no import needed (set the time zone first; the machine runs on UTC−3):

```bash
uv run python -c "import duckdb; duckdb.sql(\"SET TimeZone='UTC'\"); print(duckdb.sql(\"
  SELECT tx_type, count(*) n FROM '/local/data/xrpl/transactions/*.parquet' GROUP BY 1 ORDER BY n DESC\"))"
```

The status page (`report/build_export_report.py` + `report/export_template.html`) shows the goal and
backfill progress, the validator's health, days on disk, and statistics for the latest 24 hours on
disk. It computes whole-dataset totals from the small ledger files only, so an hourly build stays
cheap on the validator host.

## History of this work

**27 Sep 2026: spike.** We checked whether this machine could host XRPL history. Findings:
- Full history as an xrpld database is ~39 TB and needs SSDs. The 18 TB array is HDD and ~10× too slow for that.
- As Parquet, the whole chain is only ~2.3–2.8 TB and suits the HDD array well.
- A first 24-hour export (22,159 ledgers, 3.0M transactions) was complete and consistent: XRP burned
  matched fees paid.

See the [spike report](docs/spike-2026-09-28-xrpl-history.md).

**27–28 Sep: faster, verified fetching.**
- **Public servers.** Probed every public mainnet endpoint we could find. Seven served old ledgers. Tatum
  has only recent history; Uniblock and dRPC gave no usable answer.
- **Round-robin pool.** A pool over six of them raised the speed from 10.6 to ~25–31 ledgers/s.
- **Hash checks** on every header and chunk.
- **Binary responses** were measured and dropped: a third smaller, but no faster, because the servers'
  own work dominates.
- **Batched requests** don't exist on the XRPL. The API takes one request per call, and every server
  refused or failed batches.

**28 Sep, 02:28 UTC: 12-month backfill started**, newest first.
- 02:34–04:10: paused for a benchmark. The wait for it to stop matched its own shell command, so the
  restart was delayed by about 1.5 h.
- 04:10: restarted with the new dispatcher. It is as fast as the fixed pool, but adapts to each
  server on its own and uses ~0.8 GB of RAM instead of ~2.5 GB.
- 08:11: stopped after one server returned `failedToForward`, an error the exporter treated as fatal.
  Fixed: any error is now retried on another server, and only a ledger failing 30 times stops the run.

**28 Sep: status page.**
- Added speed, ETA and next update.
- Fixed progress counting across restarts.
- Redesigned it around the goal, with a timeline that fills from the newest end.
- Added "Updated … ago".

**28–29 Sep: the hourly refresh stopped at 16:00 UTC.** It had been a job inside a Claude Code session,
and ended with that session. The page froze, which made it look as if everything had stopped. The
download never did. Fixed on 29 Sep: the report build and a backfill watchdog now run from the user
crontab, and Claude only publishes.

**1 Oct 2026, 12:38 UTC: 12-month backfill finished.** All 8,136 chunks written, 7,664,000 ledgers,
788,813,053 transactions, 342 GB. `verify_export.py` and `make_datapackage.py` were not run afterward,
and no forward exporter was started, so as of 5 Oct the dataset is complete for 99,147,000–107,281,933
but ~158,000 ledgers (and growing) behind the live chain. See [open items](#open-items).

**9 Oct: silver and gold tiers** for the whole range, gap closed, dataset verified and packaged.
Dashboard rebuilt from gold.

**10–11 Oct: transfer graph.** Edge table backfilled for 14 months, then entity grouping, graph metrics
and the graph page. Two memory limits were hit on the busiest months (an ordered float sum next to other
aggregates); both are documented in `.claude/skills/xrpl-graph/SKILL.md`.

## Decisions

- **Store history as Parquet on the HDD array, not as a node database.** xrpld needs SSDs, and the
  Parquet is far smaller and easy for researchers to use.
- **Fetch history from public servers, not from the validator.** No bulk traffic goes to the
  validator. Correctness doesn't depend on the servers, because every ledger is hash-checked and
  chained.
- **No history node, Clio or Cassandra next to the validator** without a separate decision (RAM and isolation).

## Open items

- **Install `ops/forward_export.sh` in crontab** (every 10 min) so the dataset, the tiers and the
  transfer graph stay current without anyone running it.
- **Decide whether the transfer-graph tables join the published dataset** (`make_datapackage.py --with-graph`).
- **Record MPT issuance ids in silver `transfers`**; MPT payments are kept under asset `MPT` until then.
- **Raise `online_delete` to ~200,000** (~9 days of history on the validator) in the same planned
  restart, so an exporter outage of days loses nothing.
- **Check transactions against each ledger's transaction root hash.** Today only headers and the chain are verified.
- **Ask the XRPL Foundation / Ripple (UBRI) for bulk access** before any full-chain export
  (~107M ledgers, ~2 months at current speed).
- **Choose a dataset licence** (CC BY 4.0 placeholder vs CC0) and contributor names in `make_datapackage.py`.
- **Dedicated history node** on separate hardware, for deep history without public servers.

## Snapshot tool (local node)

`xrpl_snapshot.sh` reads the last N validated ledgers straight from our node. That is fine at this
size, because the node keeps about an hour of ledgers.

```bash
./xrpl_snapshot.sh          # last 20 validated ledgers
./xrpl_snapshot.sh 200      # last 200 ledgers
RPC_URL=http://host:5005 ./xrpl_snapshot.sh
```

It needs `curl` and `jq`. It writes `data/transactions_<ts>.csv`, `data/ledgers_<ts>.csv` and
`data/snapshot_<ts>.json`, plus `data/latest_*` copies. Then open `report/index.html`. All timestamps
are UTC; `delivered_xrp` is empty for issued-currency payments.
