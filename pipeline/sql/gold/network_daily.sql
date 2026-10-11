-- Network health per UTC day. lg_src holds bronze ledgers from one day before to one day after
-- the range, so the first ledger of each day has its predecessor for burn and close-gap.
WITH lg AS (
    SELECT ledger_index, close_time, tx_count, total_drops,
           lag(total_drops) OVER w - total_drops                    AS burned,   -- = fees of this ledger
           epoch(close_time) - epoch(lag(close_time) OVER w)        AS gap_s
    FROM lg_src WINDOW w AS (ORDER BY ledger_index)
),
ld AS (
    SELECT close_time::DATE AS day, count(*) AS ledgers, sum(tx_count)::BIGINT AS ledger_txs,
           sum(burned)::BIGINT AS burned_drops,
           avg(gap_s) AS close_gap_avg_s, quantile_disc(gap_s, 0.95) AS close_gap_p95_s, max(gap_s) AS close_gap_max_s,
           arg_max(ledger_index, (tx_count, -ledger_index)) AS busiest_ledger, max(tx_count) AS busiest_tx_count,
           min(ledger_index) AS first_ledger, max(ledger_index) AS last_ledger,
           min(close_time) AS first_close, max(close_time) AS last_close,
           arg_min(total_drops, ledger_index) AS supply_first, arg_max(total_drops, ledger_index) AS supply_last
    FROM lg WHERE close_time >= {day_from} AND close_time < {day_to} + 1
    GROUP BY 1
),
td AS (
    SELECT close_time::DATE AS day, count(*) AS txs,
           count(*) FILTER (WHERE result = 'tesSUCCESS') AS txs_ok,
           quantile_disc(fee_drops, 0.50) AS fee_p50,
           quantile_disc(fee_drops, 0.90) AS fee_p90,
           quantile_disc(fee_drops, 0.99) AS fee_p99,
           sum(fee_drops)::BIGINT AS fees_drops,
           count(DISTINCT account) AS active_senders,
           -- a failed tx that cannot pay its full fee burns only what the account has, so burn < fees
           count(*) FILTER (WHERE result = 'tecINSUFF_FEE') AS insuff_fee_txs
    FROM s_transactions_base GROUP BY 1
)
SELECT ld.day, ld.ledgers, coalesce(td.txs, 0) AS txs, td.txs_ok,
       100.0 * td.txs_ok / nullif(td.txs, 0) AS success_pct,
       td.fee_p50, td.fee_p90, td.fee_p99, td.fees_drops, ld.burned_drops,
       ld.close_gap_avg_s, ld.close_gap_p95_s, ld.close_gap_max_s,
       td.active_senders, td.insuff_fee_txs, ld.busiest_ledger, ld.busiest_tx_count, ld.ledger_txs,
       ld.first_ledger, ld.last_ledger, ld.first_close, ld.last_close, ld.supply_first, ld.supply_last
FROM ld LEFT JOIN td USING (day)
