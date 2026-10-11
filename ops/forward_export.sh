#!/bin/bash
# Keep the dataset current: export every validated ledger after the newest one on disk from the
# public full-history servers (never the local validator), then update silver and gold.
# Run from cron every 10 minutes. The first run after a long pause closes the whole gap
# (about 1 h per 100,000 ledgers); the lock keeps later cron runs from overlapping it.
# Each run writes a partial "tail" file (e.g. 0107281934-0107281999) that the next run continues;
# the exporter links every new file to its neighbour on disk by ledger hash.
set -u
cd "$(dirname "$0")/.." || exit 1
DATA=/local/data/xrpl
LOG=$DATA/export_forward.log
UV="${UV:-$HOME/.local/bin/uv}"
exec 9>/tmp/xrpl-forward-export.lock
flock -n 9 || exit 0

# Leave a deliberate stop alone (hash-chain break, a ledger failing everywhere) until a person looks.
if [ -f "$LOG" ] && tail -n 5 "$LOG" | grep -qE 'not written|stopping|does not link'; then
  echo "$(date -u +%FT%TZ) not running: last export stopped on purpose: $(tail -n 1 "$LOG" | cut -c1-200)" >> "$DATA/watchdog.log"
  exit 0
fi

last_file=$(ls "$DATA/ledgers" | grep -E '^[0-9]{10}-[0-9]{10}\.parquet$' | sort | tail -n 1)
[ -n "$last_file" ] || { echo "$(date -u +%FT%TZ) no ledgers on disk" >> "$LOG"; exit 1; }
last=$((10#${last_file:11:10}))

echo "$(date -u +%FT%TZ) forward export from $((last + 1))" >> "$LOG"
nice -n 10 ionice -c3 "$UV" run export_ledgers.py --start $((last + 1)) >> "$LOG" 2>&1 < /dev/null || exit 1

# Release the export lock first: the pipeline has its own lock, and a long silver backfill must
# not hold up the next export.
exec 9>&-
nice -n 10 ionice -c3 "$UV" run pipeline/run.py update >> "$DATA/pipeline/logs/cron.log" 2>&1 < /dev/null

# Transfer graph for the open month (and, on the 1st, the month that just closed), once an hour:
# the open month's inputs change on every export, and a full graph pass takes minutes. Months whose
# inputs did not change are skipped by their fingerprint, and the graph run has its own lock.
[ "$((10#$(date -u +%M)))" -lt 10 ] || exit 0
months=$(date -u +%Y-%m)
[ "$(date -u +%d)" = "01" ] && months="$(date -u -d 'yesterday' +%Y-%m),$months"
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 nice -n 10 ionice -c3 \
  "$UV" run graph/run.py all --months "$months" >> "$DATA/pipeline/logs/graph_cron.log" 2>&1 < /dev/null
