-- Keep the best candidate per record (highest `better`) across the existing table and new candidates.
SELECT record, unnest(r) FROM (
    SELECT record, arg_max(struct_pack(value, better, account, counterparty, hash, ledger_index, close_time), (better, -ledger_index)) AS r
    FROM (SELECT * FROM g_existing UNION ALL BY NAME SELECT * FROM cand)
    GROUP BY record)
