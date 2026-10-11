# Where XRPL history can come from (findings, Sep 2026)

Summary of the 2026-09-28 spike (`docs/spike-2026-09-28-xrpl-history.md`). Read before proposing a
local history node, Clio, or a config change on the validator.

| Option | Verdict | Why |
|---|---|---|
| Local validator's own database | ❌ for history | `online_delete=512` keeps ~1 h. Raising it means a restart plus heavy backfill on a live validator. |
| xrpld history node on the HDD array | ❌ | NuDB requires SSD. Measured HDD: 188 random reads/s single, ~1,150/s max; busy servers need >10,000/s. The node would fall behind the network. |
| xrpld history node on the NVMe | ⚠️ ~9 months max | 1 year ≈ 4.5 TB in NuDB; 3.7 TB free, shared with the validator. 31 GB RAM vs 64 GB recommended. |
| Clio + Cassandra on the HDD array | ⚠️ storage OK, source not | Full history ~1.9 TB (Aug 2023); Cassandra runs on HDD (commit log on NVMe). But Clio has **no backfill**: it ingests forward from an xrpld that already holds the ledgers. |
| Public full-history RPC → Parquet | ✅ today | ~9–10 ledgers/s, no validator load. 1 year ≈ 10 days and ~500 GB. Throttled; be polite. |
| Forward export from the local node | ✅ proposed | One request per new ledger (~every 4 s). Removes public servers from today on. Must run at least hourly (1 h local window). |
| Dedicated history node (separate machine, 64 GB RAM, NVMe, XFS) | ✅ long term | Full independence; doubles as the validator proxy xrpl.org recommends. Full history ~39 TB and months of peer sync. |

## Sources

- https://xrpl.org/docs/infrastructure/installation/capacity-planning
- https://xrpl.org/docs/infrastructure/configuration/data-retention/configure-full-history
- https://xrpl.org/docs/infrastructure/configuration/server-modes/run-xrpld-as-a-validator
- https://xrpl.org/docs/concepts/networks-and-servers/the-clio-server
- https://github.com/XRPLF/clio and https://github.com/XRPLF/clio/discussions/361
- https://cassandra.apache.org/doc/3.11/cassandra/operating/hardware.html
