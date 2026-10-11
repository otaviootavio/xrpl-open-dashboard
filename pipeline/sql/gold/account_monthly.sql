-- Activity per month and sending account.
SELECT date_trunc('month', close_time)::DATE AS month, account,
       count(*) AS txs,
       count(*) FILTER (WHERE result = 'tesSUCCESS') AS txs_ok,
       sum(fee_drops)::BIGINT AS fees_drops,
       (sum(delivered_drops) FILTER (WHERE tx_type = 'Payment' AND result = 'tesSUCCESS'))::BIGINT AS sent_drops
FROM s_transactions_base
GROUP BY 1, 2
