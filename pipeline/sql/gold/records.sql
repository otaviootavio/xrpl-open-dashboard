-- Record candidates from newly processed chunks. `better` orders candidates: higher wins.
SELECT * FROM (
    SELECT 'biggest_xrp_payment' AS record, drops::DOUBLE AS value, drops::DOUBLE AS better,
           from_account AS account, to_account AS counterparty, hash, ledger_index, close_time
    FROM s_transfers WHERE asset = 'XRP' AND NOT is_self ORDER BY drops DESC, ledger_index, hash LIMIT 1)
UNION ALL
SELECT * FROM (
    SELECT 'record_fee', fee_drops, fee_drops, account, NULL, hash, ledger_index, close_time
    FROM s_transactions_base ORDER BY fee_drops DESC, ledger_index, hash LIMIT 1)
UNION ALL
SELECT * FROM (
    SELECT 'busiest_ledger', tx_count, tx_count, NULL, NULL, NULL, ledger_index, close_time
    FROM lg_src ORDER BY tx_count DESC, ledger_index LIMIT 1)
UNION ALL
SELECT * FROM (
    SELECT 'quietest_ledger', tx_count, -tx_count, NULL, NULL, NULL, ledger_index, close_time
    FROM lg_src ORDER BY tx_count ASC, ledger_index LIMIT 1)
UNION ALL
SELECT * FROM (
    SELECT 'longest_close_gap', gap_s, gap_s, NULL, NULL, NULL, ledger_index, close_time
    FROM (SELECT ledger_index, close_time,
                 epoch(close_time) - epoch(lag(close_time) OVER (ORDER BY ledger_index)) AS gap_s FROM lg_src)
    WHERE gap_s IS NOT NULL ORDER BY gap_s DESC, ledger_index LIMIT 1)
UNION ALL
SELECT * FROM (
    SELECT 'smallest_xrp_payment', drops::DOUBLE, -drops::DOUBLE, from_account, to_account, hash, ledger_index, close_time
    FROM s_transfers WHERE asset = 'XRP' AND tx_type = 'Payment' AND drops > 0 ORDER BY drops, ledger_index, hash LIMIT 1)
