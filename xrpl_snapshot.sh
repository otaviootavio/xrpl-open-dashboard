#!/usr/bin/env bash
# xrpl_snapshot.sh — read a local rippled/xrpld node and export an open-data snapshot.
#
# Usage: ./xrpl_snapshot.sh [NUM_LEDGERS]      (default: 20 most recent validated ledgers)
# Env:   RPC_URL (default http://127.0.0.1:5005)
#
# Outputs (in ./data, timestamped + "latest" copies):
#   snapshot_<ts>.json      server info, fees, per-ledger stats, tx-type counts
#   ledgers_<ts>.csv        one row per ledger
#   transactions_<ts>.csv   one row per transaction
# and report/data.js, which report/index.html renders.
set -euo pipefail

N="${1:-20}"
RPC_URL="${RPC_URL:-http://127.0.0.1:5005}"
DIR="$(cd "$(dirname "$0")" && pwd)"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

for bin in curl jq; do
  command -v "$bin" >/dev/null || { echo "error: $bin is required" >&2; exit 1; }
done

rpc() { # rpc <method> [params-json]
  curl -sf -m 30 -X POST -H 'Content-Type: application/json' \
    -d "{\"method\":\"$1\",\"params\":[${2:-{\}}]}" "$RPC_URL"
}

echo "» Connecting to $RPC_URL"
rpc server_info > "$TMP/server_info.json" || { echo "error: node not reachable at $RPC_URL" >&2; exit 1; }
rpc fee > "$TMP/fee.json"

LAST=$(jq -r '.result.info.validated_ledger.seq' "$TMP/server_info.json")
FIRST=$((LAST - N + 1))
echo "» Node state: $(jq -r '.result.info.server_state' "$TMP/server_info.json"), validated ledger #$LAST"
echo "» Fetching ledgers $FIRST..$LAST"

: > "$TMP/ledgers.jsonl"
for ((i = FIRST; i <= LAST; i++)); do
  printf '\r  ledger %d (%d/%d)' "$i" $((i - FIRST + 1)) "$N"
  rpc ledger "{\"ledger_index\":$i,\"transactions\":true,\"expand\":true}" \
    | jq -c '.result.ledger' >> "$TMP/ledgers.jsonl"
done
echo

# Per-transaction rows (compact) — shared input for CSV and aggregates.
jq -c '. as $l | .transactions[] | {
    ledger_index: ($l.ledger_index|tonumber),
    close_time: $l.close_time_iso,
    hash, type: .TransactionType, account: .Account,
    destination: (.Destination // ""),
    fee_drops: (.Fee|tonumber),
    result: .metaData.TransactionResult,
    delivered_xrp: (if (.metaData.delivered_amount|type) == "string"
                    then (.metaData.delivered_amount|tonumber/1e6) else null end)
  }' "$TMP/ledgers.jsonl" > "$TMP/txs.jsonl"

jq -s --slurpfile si "$TMP/server_info.json" --slurpfile fee "$TMP/fee.json" \
      --slurpfile txs <(jq -s . "$TMP/txs.jsonl") --arg ts "$TS" --arg rpc "$RPC_URL" '
  ($si[0].result.info) as $info | ($txs[0]) as $t |
  {
    generated_at: $ts,
    source: $rpc,
    node: {
      build_version: $info.build_version, server_state: $info.server_state,
      peers: $info.peers, uptime_s: $info.uptime, complete_ledgers: $info.complete_ledgers,
      validation_quorum: $info.validation_quorum, proposers: $info.last_close.proposers,
      converge_time_s: $info.last_close.converge_time_s, load_factor: $info.load_factor,
      warnings: [($info.warnings // [])[] | .message]
    },
    fees: {
      base_fee_xrp: $info.validated_ledger.base_fee_xrp,
      reserve_base_xrp: $info.validated_ledger.reserve_base_xrp,
      reserve_inc_xrp: $info.validated_ledger.reserve_inc_xrp,
      drops: $fee[0].result.drops
    },
    ledgers: [ .[] | {
      ledger_index: (.ledger_index|tonumber), hash: .ledger_hash,
      close_time: .close_time_iso, total_xrp: (.total_coins|tonumber/1e6),
      tx_count: (.transactions|length),
      tx_success: ([.transactions[] | select(.metaData.TransactionResult=="tesSUCCESS")]|length)
    } ],
    tx_types: ($t | group_by(.type) | map({type: .[0].type, count: length,
                success: (map(select(.result=="tesSUCCESS"))|length)}) | sort_by(-.count)),
    results: ($t | group_by(.result) | map({result: .[0].result, count: length}) | sort_by(-.count)),
    totals: {
      transactions: ($t|length),
      unique_accounts: ($t | map(.account) | unique | length),
      fees_burned_xrp: (($t | map(.fee_drops) | add // 0) / 1e6),
      xrp_delivered: ($t | map(select(.type=="Payment") | .delivered_xrp // 0) | add // 0)
    }
  }' "$TMP/ledgers.jsonl" > "$DIR/data/snapshot_$TS.json"

jq -r '.ledgers | (["ledger_index","hash","close_time","total_xrp","tx_count","tx_success"]),
       (.[] | [.ledger_index,.hash,.close_time,.total_xrp,.tx_count,.tx_success]) | @csv' \
  "$DIR/data/snapshot_$TS.json" > "$DIR/data/ledgers_$TS.csv"

{ echo '"ledger_index","close_time","hash","type","account","destination","fee_drops","result","delivered_xrp"'
  jq -r '[.ledger_index,.close_time,.hash,.type,.account,.destination,.fee_drops,.result,.delivered_xrp] | @csv' "$TMP/txs.jsonl"
} > "$DIR/data/transactions_$TS.csv"

for f in snapshot.json ledgers.csv transactions.csv; do
  cp "$DIR/data/${f%.*}_$TS.${f##*.}" "$DIR/data/latest_$f"
done
{ printf 'window.SNAPSHOT = '; cat "$DIR/data/snapshot_$TS.json"; printf ';\n'; } > "$DIR/report/data.js"

jq -r '"» Done: \(.totals.transactions) txs across \(.ledgers|length) ledgers, \(.totals.unique_accounts) accounts"' \
  "$DIR/data/snapshot_$TS.json"
echo "  data:   $DIR/data/*_$TS.*"
echo "  report: $DIR/report/index.html"
