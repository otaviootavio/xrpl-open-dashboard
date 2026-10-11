# Spike: can we host XRPL mainnet history ourselves?

| | |
|---|---|
| **Date** | 27–28 September 2026 |
| **Machine** | our XRPL mainnet validator host |
| **Project** | XRPL Open Data Project |
| **Status** | Complete |
| **Dataset** | `/local/data/xrpl` (Data Package v2, see `datapackage.json`) |

## 1. Experiment

**Question.** Using the machine that runs our validator, can we build an open research dataset of
XRP Ledger mainnet, and could that machine hold the full ledger history?

**Setup**

- **Machine:** Intel Core i5-9400F (6 cores), 31 GB RAM. Disks: 3.6 TB NVMe SSD (`/validatorfs`, xrpld
  database), 18 TB RAID1 of two 20 TB HDDs (`/local/data`, empty at start), 98 GB system disk. All ext4.
- **Node:** xrpld, a proposing validator (`validating: true`, `proposing: true`, one of 35
  proposers; quorum 29), configured with `online_delete=512`.
- **Tools:** `xrpl_snapshot.sh` (Bash + jq, reads our node); `export_ledgers.py` (Python, httpx,
  PyArrow; writes zstd Parquet); DuckDB for analysis; `report/build_export_report.py` for the HTML
  report; `make_datapackage.py` for the Data Package metadata. Python environment managed with uv.
- **Data sources:** our node for live state; the public full-history servers `s2.ripple.com` and
  `xrplcluster.com` for history, so the validator carried no extra load.

**Steps**

1. Took snapshots of the 20 latest validated ledgers from our node and published a first report.
2. Inspected the node's configuration, disks and memory to see what history we hold and could hold.
3. Exported the last 24 hours of ledgers, with every transaction and its metadata, to Parquet on the
   HDD array.
4. Sampled 96 ledgers across 2013–2025 to estimate the size of the full history.
5. Measured random-read speed of the HDD array.
6. Checked findings against xrpl.org guidance on full history, capacity planning, validators and Clio.
7. Packaged the dataset in the Open Knowledge Foundation's Data Package v2 format and validated it with
   `frictionless`.

## 2. Facts

### Data collected

| Dataset | Contents | Size |
|---|---|---|
| 24 h export | Ledgers #107,259,775 – #107,281,933 (22,159 ledgers, 2026-09-27 00:56:40 → 2026-09-28 00:56:40 UTC), 3,013,522 transactions, raw `tx_json` and `meta_json` included | 1.35 GB |
| Node snapshots | Three runs of the 20 latest validated ledgers from our node, CSV and JSON (`data/`) | < 5 MB |
| History sample | 8 random ledgers at each of 12 points, ledger 1M (2013) to 100M (2025), transaction counts | – |
| Infrastructure | Node config, disk layout, RAM, CPU, node state; HDD random-read benchmark | – |

The export fetched 21,866 ledgers from s2.ripple.com and 293 from xrplcluster.com in 44 minutes,
with 1,632 throttled requests retried.

### Data analysed

Transaction types, engine result codes, hourly throughput, most active accounts, issued-currency
payments, XRP burned versus fees paid, bytes stored per ledger and per transaction, public-server
fetch rate and throttling, and transactions per ledger across the chain's history.

### Observations

- **Load.** 34.9 transactions per second on average, 136 per ledger, one ledger every 3.90 s.
  19,092 distinct accounts sent at least one transaction.
- **Failures are common.** 27.6% of included transactions failed, mostly `tecPATH_DRY` (262,716),
  `tecKILLED` (259,139) and `tecUNFUNDED_OFFER` (174,257). They still pay fees and stay in the ledger.
- **The DEX dominates.** Offer transactions were 44.5% of all traffic. The top three senders are
  market-making bots (280,037, 92,657 and 83,918 transactions).
- **Transaction mix.** Payment 1,235,895; OfferCreate 1,204,254; TicketCreate 190,780; OfferCancel
  137,429; CheckCash 96,789; NFTokenCreateOffer 54,661.
- **RLUSD leads token payments:** 515,713 successful payments, against 16,243 for the next token (C9BR).
- **Native XRP moved:** 416.8M XRP delivered in Payments.
- **The export is complete and consistent.** No gaps (22,159 ledgers over a 22,159-ledger range), no
  duplicate transaction hashes, and every ledger's `tx_count` matches its exported rows. The drop in
  total supply (727.1660 XRP burned) matches the fees paid (727.1673 XRP); the gap is the first
  ledger's own fees.
- **History grew steadily.** About 1 transaction per ledger in 2013, 20–50 in 2014–2019, 50–63 in
  2020–2023, 75–83 in 2024–2025 and 136 today. That puts the whole chain at roughly 5.3 billion
  transactions (rough estimate from a small sample).
- **Parquet is compact.** 60.9 KB per ledger, against about 450 KB per ledger in xrpld's own database
  (4.5 TB per 10M ledgers, per xrpl.org). The full transaction history would be roughly 2.3–2.8 TB in
  Parquet.
- **Public servers are usable but throttled.** About 8–10 ledgers/s. xrplcluster.com answered mostly
  with `slowDown`/`tooBusy`.
- **Our node keeps about 1,000 ledgers** (about an hour) because of `online_delete=512`.
- **The HDD array is slow at random reads.** Measured with 4 KB direct reads on an 8 GB file:
  188 reads/s one at a time (5.3 ms each), 902/s with 16 in parallel, 1,149/s with 64 in parallel.
  xrpl.org reports busy servers above 10,000 reads/s.
- **The validator dropped out once.** It restarted during a package install and was out of consensus
  for about 3 minutes.
- **An upgrade is due.** The node warns that unsupported amendments have reached majority, with
  activation expected on 2026-10-09.

## 3. Problem hypotheses

Issues that, as things stand, stop us from keeping the full ledger on this machine.
*Measured* = observed in this spike; *documented* = from xrpl.org or vendor docs, not tested here;
*suspected* = cause not confirmed.

| # | Problem | Evidence | Basis |
|---|---|---|---|
| P1 | Not enough SSD | Full history is about 39 TB in xrpld's database and grows about 12 GB/day; one year is about 4.5 TB. We have 3.7 TB free on the NVMe, about 9 months. | measured, documented |
| P2 | The big disks are HDDs | xrpld's NuDB database requires SSD. Our array does about 1,150 random reads/s at best, roughly a tenth of what busy servers need. A node on it would fall behind the network. | measured, documented |
| P3 | File size limit | Both disks are ext4 (16 TB per file). Full-history NuDB needs XFS or ZFS. | measured, documented |
| P4 | Not enough RAM | 31 GB total, about 15 GB used by the validator. xrpl.org recommends 64 GB for one production server. | measured, documented |
| P5 | Validator isolation | The node is an active validator. xrpl.org warns that heavy API load makes validators less reliable and recommends separate stock servers in front of them. One restart already cost about 3 minutes of consensus; we suspect the package install caused it. | documented, suspected |
| P6 | Acquisition time | Syncing full history from peers takes several months (xrpl.org). Fetching 107M ledgers from public servers at about 10/s takes about 4 months, and they throttle bulk use. | measured, documented |
| P7 | Clio does not solve history | Clio stores history compactly (about 1.9 TB for full history in Aug 2023) and Cassandra runs on HDDs, but Clio has no backfill: it only ingests forward from an xrpld that already holds the ledgers. | documented |
| P8 | Pending upgrade | If xrpld is not upgraded before about 2026-10-09, the node becomes amendment-blocked. | measured |

## 4. Solution hypotheses

| # | Next step | Hypothesis | Success looks like |
|---|---|---|---|
| S1 | Parquet archive on the HDD array | Store history as Parquet, not as a node database. It is about 7× smaller, suits sequential HDD access, and researchers query it directly. Addresses P1–P3. | **Validated:** 24 h exported, consistent, and packaged as a valid Data Package. |
| S2 | Forward export from our own node | Every 10 minutes, fetch new validated ledgers from `127.0.0.1:5005` (one request per ~4 s). No public servers from today on, about 1.2–1.3 GB/day. | No gaps in the calendar for 30 days, no measurable change in validator performance. |
| S3 | Small history buffer on the validator | Raise `online_delete` to about 200,000 (~9 days, ~90 GB), in one planned restart, so exporter outages of days lose nothing. | The exporter can be offline for a week and catch up from our node. |
| S4 | One-time 12-month backfill | Export the last year from public servers, month by month, at a lower concurrency: about 8.3M ledgers, about 10 days, about 500 GB. | 12 full months on disk with no rate-limit bans. |
| S5 | Dedicated history node | Separate hardware (64 GB RAM, large NVMe, XFS) running a non-validating stock server with deep history. It feeds the exporter, can host Clio later, and doubles as the proxy xrpl.org recommends in front of the validator. Addresses P4–P7. A second NVMe (4 TB+) in this machine is the cheaper, partial alternative. | Deep history without public servers; validator untouched. |
| S6 | Ledger state snapshots | Pilot one `ledger_data` dump (accounts, trust lines, offers, AMM pools) to measure size and choose a schedule. | Snapshot size known, schedule chosen. |
| S7 | Protect the validator | Upgrade xrpld before 2026-10-09; schedule package installs so restarts only happen when planned. Addresses P8. | No amendment block; no unplanned restarts. |
| S8 | Publish the dataset | Choose a licence, add a download route, and share the Data Package with researchers. | A researcher outside the team runs a query without our help. |

## References

- Configure Full History — https://xrpl.org/docs/infrastructure/configuration/data-retention/configure-full-history
- Capacity Planning — https://xrpl.org/docs/infrastructure/installation/capacity-planning
- Run xrpld as a Validator — https://xrpl.org/docs/infrastructure/configuration/server-modes/run-xrpld-as-a-validator
- Run xrpld as a Stock Server — https://xrpl.org/docs/infrastructure/configuration/server-modes/run-xrpld-as-a-stock-server
- The Clio Server — https://xrpl.org/docs/concepts/networks-and-servers/the-clio-server
- Clio README — https://github.com/XRPLF/clio
- Clio and rippled config, discussion #361 — https://github.com/XRPLF/clio/discussions/361
- Apache Cassandra hardware choices — https://cassandra.apache.org/doc/3.11/cassandra/operating/hardware.html
- Data Package v2 — https://datapackage.org
