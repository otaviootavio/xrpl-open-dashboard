-- One row per (month, sender, receiver, asset): the directed, weighted transfer graph.
-- Self-transfers are left out. drops is exact for XRP; value_f is in the asset's own units.
-- The ordered float sum runs in its own aggregation: next to the other aggregates in one GROUP BY it
-- ran out of memory under the 4 GB cap on months with a 1.4 M-transfer pair (2026-02, 2026-03).
WITH t AS (
    -- MPT payments have no currency/issuer, so silver leaves `asset` NULL (1,378 in the first year):
    -- they are kept under 'MPT' until silver records the mpt_issuance_id.
    SELECT date_trunc('month', close_time)::DATE AS month, * REPLACE (coalesce(asset, 'MPT') AS asset)
    FROM s_transfers WHERE NOT is_self
),
v AS (
    SELECT month, from_account, to_account, asset, sum(value_f ORDER BY value_f) AS value_f
    FROM t GROUP BY ALL
),
c AS (
    SELECT month, from_account, to_account, asset,
           any_value(currency_code)                                   AS currency_code,
           count(*)::BIGINT                                           AS n_transfers,
           sum(drops)::BIGINT                                         AS drops,
           count(*) FILTER (WHERE partial_payment)::BIGINT            AS n_partial,
           count(destination_tag)::BIGINT                             AS n_tagged,
           count(DISTINCT destination_tag)::BIGINT                    AS n_distinct_tags,
           count(*) FILTER (WHERE tx_type = 'Payment')::BIGINT        AS n_payment,
           count(*) FILTER (WHERE tx_type = 'CheckCash')::BIGINT      AS n_checkcash,
           count(*) FILTER (WHERE tx_type = 'EscrowFinish')::BIGINT   AS n_escrowfinish,
           min(ledger_index)                                          AS first_ledger,
           max(ledger_index)                                          AS last_ledger
    FROM t GROUP BY ALL
)
SELECT c.month, c.from_account, c.to_account, c.asset, c.currency_code, c.n_transfers, c.drops, v.value_f,
       c.n_partial, c.n_tagged, c.n_distinct_tags, c.n_payment, c.n_checkcash, c.n_escrowfinish,
       c.first_ledger, c.last_ledger
FROM c JOIN v USING (month, from_account, to_account, asset)
