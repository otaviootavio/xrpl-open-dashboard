-- Whole-month network figures, computed from transactions (not averaged from daily percentiles).
SELECT date_trunc('month', close_time)::DATE AS month,
       count(*) AS txs,
       count(*) FILTER (WHERE result = 'tesSUCCESS') AS txs_ok,
       100.0 * count(*) FILTER (WHERE result = 'tesSUCCESS') / count(*) AS success_pct,
       quantile_disc(fee_drops, 0.50) AS fee_p50,
       quantile_disc(fee_drops, 0.90) AS fee_p90,
       quantile_disc(fee_drops, 0.99) AS fee_p99,
       sum(fee_drops)::BIGINT AS fees_drops,
       count(DISTINCT account) AS active_senders,
       count(*) FILTER (WHERE tx_type = 'OfferCreate') AS offer_creates,
       count(*) FILTER (WHERE tx_type = 'OfferCancel') AS offer_cancels,
       count(*) FILTER (WHERE result = 'tecPATH_DRY') AS tec_path_dry,
       count(*) FILTER (WHERE result = 'tecUNFUNDED_OFFER') AS tec_unfunded_offer
FROM s_transactions_base
GROUP BY 1
