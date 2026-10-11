-- One row per transaction: the typed bronze columns, without tx_json / meta_json.
SELECT ledger_index, close_time, tx_index, hash, tx_type, account, destination, destination_tag,
       sequence, fee_drops, result, delivered_drops, delivered_currency, delivered_issuer,
       delivered_value, affected_nodes
FROM src
