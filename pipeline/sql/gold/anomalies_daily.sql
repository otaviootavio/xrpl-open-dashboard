-- Accounts with unusual days: at least 1,000 failed (tec*) transactions, or a fee of 1 XRP or more.
SELECT close_time::DATE AS day, account,
       count(*) AS tx_count,
       count(*) FILTER (WHERE starts_with(result, 'tec')) AS tec_count,
       max(fee_drops) AS fee_drops_max,
       count(*) FILTER (WHERE starts_with(result, 'tec')) >= 1000 AS flood,
       max(fee_drops) >= 1000000 AS fee_spike
FROM s_transactions_base
GROUP BY 1, 2
HAVING flood OR fee_spike
