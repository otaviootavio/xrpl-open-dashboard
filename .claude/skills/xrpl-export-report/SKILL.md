---
name: xrpl-export-report
description: Rebuild the XRPL export report (report/export.html) from the Parquet data, server stats and export log, then republish it to its claude.ai link. Use when asked to update, refresh or republish the XRPL report or dashboard, to add a section or tab to it, or on the hourly refresh.
license: CC-BY-4.0
compatibility: Designed for Claude Code on a host that also runs a live XRPL validator (reads /local/data/xrpl, /proc and the local xrpld RPC). Publishing needs the Artifact tool.
metadata:
  project: xrpl-open-data-project
  version: "1.0"
---

# XRPL export report

`report/build_export_report.py` queries the Parquet files with DuckDB, reads live server stats
(CPU, memory, disks, xrpld `server_info`) and the exporter log, and inlines everything into
`report/export_template.html`, producing a single self-contained `report/export.html`.

The page has two tabs: **Export status** (progress, server panel, 12-month coverage calendar,
tiles, hourly chart, types, result codes, top accounts, token payments, files) and **Spike report**
(experiment, facts, problem and solution hypotheses; its numbers come from the data).

## Steps

1. Rebuild from the project root:
   ```bash
   uv run report/build_export_report.py            # --log <file> if the run used another log name
   ```
   It prints the ledger and transaction counts it included. It takes ~5 s (it samples CPU for 1 s).
2. Sanity-check the numbers against the data (the builder already freezes the file list, so totals are consistent).
3. Publish with the Artifact tool, **same file path** `report/export.html`, so the link stays
   the status-page link in AGENTS.local.md. Give the publish a short label (e.g. the UTC hour).
   Omit `icon` on republish. From a new conversation, pass that URL as `url` and read it first.

## Editing the page

- Change layout, charts or copy in `report/export_template.html`; change data in `build_export_report.py`
  (add a key to the `data` dict, then use it as `D.<key>` in the template).
- The template is a page fragment: it starts with `<title>` and has no `<html>/<head>/<body>`, as the
  Artifact publisher wraps it. Keep light and dark tokens in sync (`:root`, the dark media query, and `[data-theme="dark"]`).
- Charts are hand-written SVG with hover tooltips and a table fallback; series colours come from
  `--s1` (succeeded) and `--s2` (failed). Keep one axis per chart.
- There is no browser on this host. After editing JS, at least parse it:
  ```bash
  uv run --with esprima python -c "import re,esprima; h=open('report/export.html').read(); [esprima.parseScript(j.replace('??','||').replace('?.','.')) for j in re.findall(r'<script>(.*?)</script>', h, re.S)]; print('JS OK')"
  ```

## Edge cases

- The page is private; sharing happens from its Share menu. It shows the hostname and hardware:
  point that out before the user shares it.
- If xrpld is restarting, the server panel shows "Not reachable" instead of failing the build.
- Hourly refresh in Claude Code: a session-only CronCreate job (expires after 7 days, dies with the
  session). A system cron job can rebuild the HTML, but only a Claude session can publish it.
