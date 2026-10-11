-- One row per account created (AccountRoot created) or deleted (AccountRoot deleted).
SELECT n.ledger_index, n.close_time, n.hash, n.tx_type,
       CASE n.node_kind WHEN 'CreatedNode' THEN 'created' ELSE 'deleted' END            AS event,
       coalesce(n.node ->> '$.NewFields.Account', n.node ->> '$.FinalFields.Account')     AS account,
       CASE WHEN n.node_kind = 'CreatedNode' THEN (n.node ->> '$.NewFields.Balance')::BIGINT END AS initial_drops,
       CASE WHEN n.node_kind = 'CreatedNode' THEN n.account END                          AS funded_by,
       CASE WHEN n.node_kind = 'DeletedNode' THEN n.destination END                      AS remainder_to,
       json_exists(coalesce(n.node -> '$.NewFields', n.node -> '$.FinalFields'), '$.AMMID') AS is_amm
FROM nodes n
WHERE n.entry_type = 'AccountRoot' AND n.node_kind IN ('CreatedNode', 'DeletedNode') AND n.result = 'tesSUCCESS'
