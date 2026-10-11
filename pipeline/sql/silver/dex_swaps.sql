-- One row per consumed order-book offer or AMM pool swap leg. `taker` is the transaction's
-- account; out = what the taker received (the maker or pool gave), in = what the taker paid.
WITH ob AS (
    SELECT ledger_index, close_time, tx_index, hash, tx_type, account AS taker,
           node ->> '$.FinalFields.Account'                                         AS maker,
           node -> '$.PreviousFields.TakerGets'                                     AS pg,
           node -> '$.FinalFields.TakerGets'                                        AS fg,
           coalesce(node -> '$.PreviousFields.TakerPays', node -> '$.FinalFields.TakerPays') AS pp,
           node -> '$.FinalFields.TakerPays'                                        AS fp
    FROM nodes
    WHERE entry_type = 'Offer' AND node_kind IN ('ModifiedNode', 'DeletedNode')
      AND result = 'tesSUCCESS' AND json_exists(node, '$.PreviousFields.TakerGets')
),
book AS (
    SELECT ledger_index, close_time, tx_index, hash, tx_type, 'orderbook' AS venue, maker, taker, NULL::VARCHAR AS amm_id,
           amt_asset(fg) AS asset_out, amt_value(pg) - amt_value(fg) AS amount_out,
           amt_asset(fp) AS asset_in,  amt_value(pp) - amt_value(fp) AS amount_in
    FROM ob
),
amm AS (
    SELECT ledger_index, close_time, tx_index, hash, tx_type, 'amm' AS venue, pool AS maker, account AS taker, amm_id,
           arg_min(asset, delta) AS asset_out, -min(delta) AS amount_out,
           arg_max(asset, delta) AS asset_in,   max(delta) AS amount_in
    FROM pool_deltas
    WHERE result = 'tesSUCCESS' AND NOT starts_with(tx_type, 'AMM')
    GROUP BY ledger_index, close_time, tx_index, hash, tx_type, pool, account, amm_id
    HAVING min(delta) < 0 AND max(delta) > 0
)
SELECT * FROM (SELECT * FROM book UNION ALL SELECT * FROM amm)
WHERE amount_out > 0 AND amount_in > 0
