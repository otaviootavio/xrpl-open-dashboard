-- One row per successful AMM transaction (create, deposit, withdraw, bid, vote, delete, clawback).
WITH obj AS (
    SELECT hash, node_kind,
           coalesce(node -> '$.FinalFields', node -> '$.NewFields') AS f,
           node -> '$.PreviousFields' AS pf
    FROM nodes WHERE entry_type = 'AMM'
),
amm AS (
    SELECT hash,
           f ->> 'Account'      AS pool,
           issue_key(f -> 'Asset')  AS asset1,
           issue_key(f -> 'Asset2') AS asset2,
           f -> 'LPTokenBalance' ->> 'currency' AS lp_currency,
           CASE WHEN node_kind = 'DeletedNode' THEN 0 ELSE (f -> 'LPTokenBalance' ->> 'value')::DOUBLE END
         - CASE WHEN node_kind = 'CreatedNode' THEN 0
                ELSE coalesce(pf -> 'LPTokenBalance' ->> 'value', f -> 'LPTokenBalance' ->> 'value')::DOUBLE END AS lp_delta,
           (f ->> 'TradingFee')::INT AS trading_fee
    FROM obj
),
deltas AS (
    SELECT hash, pool, any_value(amm_id) AS amm_id, map(list(asset), list(delta)) AS m FROM pool_deltas GROUP BY hash, pool
)
SELECT t.ledger_index, t.close_time, t.hash, t.tx_type,
       lower(replace(t.tx_type, 'AMM', '')) AS event,
       t.account,
       coalesce(a.pool, d.pool) AS pool, d.amm_id,
       a.asset1, a.asset2,
       d.m[a.asset1] AS asset1_delta,
       d.m[a.asset2] AS asset2_delta,
       a.lp_delta, a.trading_fee
FROM src t
LEFT JOIN amm a USING (hash)
LEFT JOIN deltas d ON d.hash = t.hash AND (a.pool IS NULL OR d.pool = a.pool)
WHERE t.result = 'tesSUCCESS' AND starts_with(t.tx_type, 'AMM')
