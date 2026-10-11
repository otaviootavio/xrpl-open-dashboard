---
name: xrpl-ledger-export
description: Export XRP Ledger mainnet ledgers and transactions to Parquet under /local/data/xrpl, verify the result, and refresh the Data Package metadata. Use when asked to sync, export, backfill or download XRPL history (last N hours, a ledger range, a month), to check an export for gaps, or to update datapackage.json / SHA256SUMS.
license: CC-BY-4.0
compatibility: Designed for Claude Code on a host that also runs a live XRPL validator. Requires uv, network access to the public full-history servers in ENDPOINTS (s2/s1.ripple.com, honeycluster.io, rpc.ankr.com, xrplcluster.com, xrpl.link), and write access to /local/data/xrpl.
metadata:
  project: xrpl-open-data-project
  version: "1.0"
---

# XRPL ledger export

Exports full ledgers (every transaction plus raw metadata JSON) from public full-history servers
into zstd Parquet, in 1,000-ledger chunks. Work from the project root
the project folder.

## Before you start

- **Never point bulk exports at the local validator** (`127.0.0.1:5005`). It keeps only ~1,000
  ledgers and must not be loaded with parallel requests. The exporter's endpoints are in
  `ENDPOINTS` in `export_ledgers.py`: a dispatcher sends each request to the public server with the
  lowest expected wait and adapts each server's concurrency (≤ 24 requests in flight in total); a
  throttled server is slowed and paused on its own. Every ledger header is checked against its hash, and each
  chunk must chain to its neighbours on disk, or the run stops without writing it.
- Check free space: `df -h /local/data`. Budget ~61 KB per ledger (~1.3 GB per day of history).
- Time budget: ~25–33 ledgers/s (benchmarked 2026-09-28), so a day of history takes ~12–15 min
  and a month ~8–10 h.

## Steps

1. **Run the export in the background**, logging to the dataset folder:
   ```bash
   uv run export_ledgers.py --hours 24 2>&1 | tee /local/data/xrpl/export_<label>.log
   # or: uv run export_ledgers.py --start <first> --end <last>
   ```
   Use the Bash tool with `run_in_background: true`. Progress lines appear once per finished chunk.
2. **Watch it** with a Monitor on the log, matching progress *and* failures:
   `Range|chunks to fetch|Done:|Traceback|Error|Interrupted|Killed`.
3. **If it stops**, rerun the same command. Finished chunks are skipped. With `--hours`, the window
   moves forward, so prefer `--start/--end` for resumable backfills.
4. **Verify** when it finishes:
   ```bash
   uv run .claude/skills/xrpl-ledger-export/scripts/verify_export.py
   ```
   Every check must say OK. See the script for what it checks.
5. **Refresh metadata**: `uv run make_datapackage.py` (rewrites `datapackage.json` and `SHA256SUMS`).
   Optionally validate against the standard:
   ```bash
   uvx --from 'frictionless[parquet]>=5.18' --with pandas frictionless validate \
     /local/data/xrpl/datapackage.json --type package --limit-rows 1000
   ```
6. **Report** with the xrpl-export-report skill if the published page should reflect the new data.

## Backfilling long ranges

- Use `--start/--end --newest-first`, with `--end` just below the oldest chunk on disk, so the
  dataset stays contiguous if the run stops. Keep boundaries 1,000-aligned so chunk names never
  overlap between runs. Run it detached at low priority, logging to `export_backfill.log`
  (see AGENTS.md for the exact command). Never run two exporters at once.
- To find the first ledger of a date, binary-search on `close_time` (see `ledger_at()` in
  `export_ledgers.py`). Ledger 99,000,000 closed on 2025-09-21; ~22,100 ledgers per day.
- Be polite to public servers: if retries climb fast, lower the worker counts in `ENDPOINTS`.

## Edge cases

- `slowDown` / `tooBusy` / HTTP 429–504 are retried with backoff. Any other RPC error stops the run
  on purpose: read it before retrying.
- Partial first/last chunks from `--hours` runs are named by their real range
  (e.g. `0107259775-0107259999.parquet`). A later run over the same range with `--start/--end`
  would write an overlapping full chunk: delete the partial one first.
- Why not a local history node or Clio: see [references/history-options.md](references/history-options.md).
