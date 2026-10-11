-- One row per created, modified or deleted trust line (RippleState), from the holder's side.
-- RippleState.Balance is from the low account's side: positive -> the low account holds the token.
WITH r AS (
    SELECT ledger_index, close_time, hash, tx_type, account, node_kind,
           coalesce(node -> '$.FinalFields', node -> '$.NewFields') AS f,
           node -> '$.PreviousFields' AS pf
    FROM nodes WHERE entry_type = 'RippleState' AND result = 'tesSUCCESS'
),
x AS (
    SELECT *,
           f -> 'Balance' ->> 'currency'                          AS currency,
           f -> 'LowLimit' ->> 'issuer'                           AS low,
           f -> 'HighLimit' ->> 'issuer'                          AS high,
           (f -> 'LowLimit' ->> 'value')::DOUBLE                  AS low_limit,
           (f -> 'HighLimit' ->> 'value')::DOUBLE                 AS high_limit,
           CASE WHEN node_kind = 'DeletedNode' THEN 0 ELSE (f -> 'Balance' ->> 'value')::DOUBLE END AS after_low,
           CASE WHEN node_kind = 'CreatedNode' THEN 0
                ELSE coalesce(pf -> 'Balance' ->> 'value', f -> 'Balance' ->> 'value')::DOUBLE END AS before_low
    FROM r
),
h AS (
    SELECT *,
           CASE WHEN after_low > 0 OR (after_low = 0 AND before_low > 0) THEN true
                WHEN after_low < 0 OR before_low < 0 THEN false
                ELSE low_limit > 0 OR high_limit = 0 END AS holder_is_low
    FROM x
)
SELECT ledger_index, close_time, hash, tx_type, account AS tx_account,
       CASE node_kind WHEN 'CreatedNode' THEN 'created' WHEN 'DeletedNode' THEN 'deleted' ELSE 'modified' END AS change,
       CASE WHEN holder_is_low THEN low ELSE high END                         AS holder,
       CASE WHEN holder_is_low THEN high ELSE low END                         AS issuer,
       currency,
       xrpl_currency_code(currency)                                          AS currency_code,
       currency || '.' || CASE WHEN holder_is_low THEN high ELSE low END      AS asset,
       CASE WHEN holder_is_low THEN low_limit ELSE high_limit END             AS holder_limit,
       CASE WHEN holder_is_low THEN before_low ELSE -before_low END           AS balance_before,
       CASE WHEN holder_is_low THEN after_low ELSE -after_low END             AS balance_after
FROM h
