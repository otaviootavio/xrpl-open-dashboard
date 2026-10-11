-- Shared step: one row per ledger object a transaction created, modified or deleted.
-- `node` holds the object's fields (FinalFields / PreviousFields / NewFields, LedgerEntryType).
SELECT ledger_index, close_time, tx_index, hash, tx_type, account, destination, result,
       json_keys(je)[1]                             AS node_kind,   -- CreatedNode | ModifiedNode | DeletedNode
       je -> json_keys(je)[1]                       AS node,
       (je -> json_keys(je)[1]) ->> 'LedgerEntryType' AS entry_type
FROM (SELECT ledger_index, close_time, tx_index, hash, tx_type, account, destination, result,
             unnest(json_extract(meta_json, '$.AffectedNodes[*]')) AS je
      FROM src)
