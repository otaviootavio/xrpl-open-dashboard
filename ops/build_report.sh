#!/bin/bash
# Rebuild report/export.html from the dataset and the host's state. Run from cron hourly at :05;
# a Claude Code session publishes the result a few minutes later.
cd "$(dirname "$0")/.." || exit 1
echo "$(date -u +%FT%TZ) build" >> /local/data/xrpl/report_build.log
nice -n 10 "${UV:-$HOME/.local/bin/uv}" run report/build_export_report.py >> /local/data/xrpl/report_build.log 2>&1
