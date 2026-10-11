-- One row per successful value movement between two accounts.
-- Amounts come from the delivered amount only, never from `Amount` (partial payments).
WITH checks AS (
    SELECT hash, node ->> '$.FinalFields.Account' AS sender
    FROM nodes WHERE entry_type = 'Check' AND node_kind = 'DeletedNode'
),
escrows AS (
    SELECT hash, node ->> '$.FinalFields.Account' AS sender, node ->> '$.FinalFields.Destination' AS receiver,
           node -> '$.FinalFields.Amount' AS amount
    FROM nodes WHERE entry_type = 'Escrow' AND node_kind = 'DeletedNode' AND tx_type = 'EscrowFinish'
),
paid AS (
    SELECT t.ledger_index, t.close_time, t.hash, t.tx_type,
           CASE WHEN t.tx_type = 'CheckCash' THEN c.sender ELSE t.account END     AS from_account,
           CASE WHEN t.tx_type = 'CheckCash' THEN t.account ELSE t.destination END AS to_account,
           CASE WHEN t.delivered_drops IS NOT NULL THEN 'XRP'
                ELSE t.delivered_currency || '.' || t.delivered_issuer END        AS asset,
           CASE WHEN t.delivered_drops IS NULL THEN xrpl_currency_code(t.delivered_currency) END AS currency_code,
           t.delivered_drops                                                     AS drops,
           t.delivered_value                                                     AS value,
           coalesce(t.delivered_drops / 1e6, t.delivered_value::DOUBLE)          AS value_f,
           t.destination_tag,
           coalesce((t.tx_json ->> 'Flags')::UBIGINT & 131072 <> 0, false)        AS partial_payment
    FROM src t LEFT JOIN checks c USING (hash)
    WHERE t.result = 'tesSUCCESS' AND t.tx_type IN ('Payment', 'CheckCash')
      AND (t.delivered_drops IS NOT NULL OR t.delivered_value IS NOT NULL)
),
escrowed AS (
    SELECT DISTINCT n.ledger_index, n.close_time, n.hash, n.tx_type, e.sender, e.receiver,
           amt_asset(e.amount), CASE WHEN NOT amt_is_xrp(e.amount) THEN xrpl_currency_code(e.amount ->> 'currency') END,
           amt_drops(e.amount), CASE WHEN NOT amt_is_xrp(e.amount) THEN e.amount ->> 'value' END,
           amt_value(e.amount), NULL::BIGINT, false
    FROM nodes n JOIN escrows e USING (hash)
    WHERE n.result = 'tesSUCCESS'
)
SELECT *, from_account = to_account AS is_self
FROM (SELECT * FROM paid UNION ALL SELECT * FROM escrowed)
