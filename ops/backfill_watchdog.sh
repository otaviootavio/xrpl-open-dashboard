#!/bin/bash
# Restart the 12-month backfill if it has died. Run from cron every 10 minutes.
# Leaves it stopped when it finished ("Done:") or stopped on purpose (hash-chain break, a ledger
# failing everywhere): those need a person to look at the log first.
set -u
cd "$(dirname "$0")/.." || exit 1
LOG=/local/data/xrpl/export_backfill.log
WLOG=/local/data/xrpl/watchdog.log
exec 9>/tmp/xrpl-backfill-watchdog.lock
flock -n 9 || exit 0

pgrep -f 'python export_ledgers[.]py' >/dev/null && exit 0
last_run=$(tac "$LOG" | sed '/^Range /q' | tac)   # log of the latest run only
if grep -q '^Done:' <<<"$last_run"; then exit 0; fi
if tail -n 5 "$LOG" | grep -qE 'not written|stopping|does not link'; then
  echo "$(date -u +%FT%TZ) not restarting: exporter stopped on purpose: $(tail -n 1 "$LOG")" >> "$WLOG"
  exit 0
fi
echo "$(date -u +%FT%TZ) exporter not running; restarting. Last line: $(tail -n 1 "$LOG" | cut -c1-200)" >> "$WLOG"
setsid nohup nice -n 10 ionice -c3 "${UV:-$HOME/.local/bin/uv}" run export_ledgers.py \
  --start 99147000 --end 107259774 --newest-first >> "$LOG" 2>&1 < /dev/null &
