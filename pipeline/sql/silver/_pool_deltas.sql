-- Shared step: balance changes of AMM pool accounts, one row per (transaction, pool, asset),
-- seen from the pool's side (positive = the pool received). LP tokens (currency 03...) are left out:
-- they are issued by the pool and tracked through the AMM object's LPTokenBalance instead.
WITH pools AS (
    SELECT DISTINCT hash,
           coalesce(node ->> '$.FinalFields.Account', node ->> '$.NewFields.Account') AS pool,
           coalesce(node ->> '$.FinalFields.AMMID',   node ->> '$.NewFields.AMMID')   AS amm_id
    FROM nodes
    WHERE entry_type = 'AccountRoot'
      AND (json_exists(node, '$.FinalFields.AMMID') OR json_exists(node, '$.NewFields.AMMID'))
),
xrp AS (
    SELECT n.hash, p.pool, 'XRP' AS asset,
           ( CASE WHEN n.node_kind = 'DeletedNode' THEN 0
                  ELSE coalesce(n.node ->> '$.FinalFields.Balance', n.node ->> '$.NewFields.Balance')::BIGINT END
           - CASE WHEN n.node_kind = 'CreatedNode' THEN 0
                  ELSE coalesce(n.node ->> '$.PreviousFields.Balance', n.node ->> '$.FinalFields.Balance')::BIGINT END
           ) / 1e6 AS delta
    FROM nodes n JOIN pools p
      ON n.hash = p.hash
     AND coalesce(n.node ->> '$.FinalFields.Account', n.node ->> '$.NewFields.Account') = p.pool
    WHERE n.entry_type = 'AccountRoot'
),
rs AS (
    SELECT n.hash, n.node_kind,
           coalesce(n.node -> '$.FinalFields', n.node -> '$.NewFields') AS f,
           n.node -> '$.PreviousFields' AS pf
    FROM nodes n WHERE n.entry_type = 'RippleState'
),
tok AS (
    SELECT rs.hash, p.pool,
           (f -> '$.Balance' ->> 'currency') || '.' ||
               CASE WHEN f -> '$.LowLimit' ->> 'issuer' = p.pool THEN f -> '$.HighLimit' ->> 'issuer'
                    ELSE f -> '$.LowLimit' ->> 'issuer' END AS asset,
           -- Balance is from the low account's side; flip it when the pool is the high account.
           CASE WHEN f -> '$.LowLimit' ->> 'issuer' = p.pool THEN 1 ELSE -1 END *
           ( CASE WHEN rs.node_kind = 'DeletedNode' THEN 0 ELSE (f -> '$.Balance' ->> 'value')::DOUBLE END
           - CASE WHEN rs.node_kind = 'CreatedNode' THEN 0
                  ELSE coalesce(pf -> '$.Balance' ->> 'value', f -> '$.Balance' ->> 'value')::DOUBLE END
           ) AS delta
    FROM rs JOIN pools p
      ON rs.hash = p.hash
     AND p.pool IN (f -> '$.LowLimit' ->> 'issuer', f -> '$.HighLimit' ->> 'issuer')
    WHERE NOT starts_with(f -> '$.Balance' ->> 'currency', '03')
)
SELECT t.ledger_index, t.close_time, t.tx_index, t.hash, t.tx_type, t.account, t.result,
       d.pool, p.amm_id, d.asset, d.delta
FROM (SELECT * FROM xrp UNION ALL SELECT * FROM tok) d
JOIN pools p USING (hash, pool)
JOIN (SELECT DISTINCT ledger_index, close_time, tx_index, hash, tx_type, account, result FROM nodes) t USING (hash)
WHERE d.delta <> 0
