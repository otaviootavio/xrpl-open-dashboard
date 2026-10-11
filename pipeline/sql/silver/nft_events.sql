-- One row per NFT event. Mints and burns are found by comparing every NFTokenPage a transaction
-- touched, before vs after (our meta_json has no nftoken_id field, and page splits move tokens
-- between pages, so a single page's diff is not enough). Offers come from NFTokenOffer objects.
WITH pages AS (
    SELECT ledger_index, close_time, hash, tx_type, account,
           CASE WHEN node_kind = 'CreatedNode' THEN []::VARCHAR[]
                WHEN json_exists(node, '$.PreviousFields.NFTokens')
                     THEN json_extract_string(node, '$.PreviousFields.NFTokens[*].NFToken.NFTokenID')
                ELSE json_extract_string(node, '$.FinalFields.NFTokens[*].NFToken.NFTokenID') END AS before_ids,
           CASE WHEN node_kind = 'DeletedNode' THEN []::VARCHAR[]
                WHEN node_kind = 'CreatedNode'
                     THEN json_extract_string(node, '$.NewFields.NFTokens[*].NFToken.NFTokenID')
                ELSE json_extract_string(node, '$.FinalFields.NFTokens[*].NFToken.NFTokenID') END AS after_ids
    FROM nodes WHERE entry_type = 'NFTokenPage' AND result = 'tesSUCCESS'
),
per_tx AS (
    SELECT ledger_index, close_time, hash, tx_type, account,
           flatten(list(before_ids)) AS b, flatten(list(after_ids)) AS a
    FROM pages GROUP BY ALL
),
minted AS (
    SELECT ledger_index, close_time, hash, tx_type, account, 'mint' AS event,
           unnest(list_filter(a, x -> NOT list_contains(b, x))) AS nftoken_id
    FROM per_tx
    UNION ALL
    SELECT ledger_index, close_time, hash, tx_type, account, 'burn',
           unnest(list_filter(b, x -> NOT list_contains(a, x)))
    FROM per_tx
),
offers AS (
    SELECT ledger_index, close_time, hash, tx_type, account, node_kind,
           coalesce(node -> '$.NewFields', node -> '$.FinalFields') AS f
    FROM nodes
    WHERE entry_type = 'NFTokenOffer' AND node_kind IN ('CreatedNode', 'DeletedNode') AND result = 'tesSUCCESS'
),
offer_rows AS (
    -- Brokered mode: a third account accepts a sell and a buy offer in one transaction and keeps the
    -- difference. Then the buyer is the buy offer's owner, not the transaction's account (the broker).
    SELECT *, coalesce((f ->> 'Flags')::INT, 0) & 1 = 1 AS is_sell,
           bool_or(coalesce((f ->> 'Flags')::INT, 0) & 1 = 1) OVER (PARTITION BY hash, node_kind) AS tx_has_sell,
           max(f ->> 'Owner') FILTER (WHERE coalesce((f ->> 'Flags')::INT, 0) & 1 = 0)
               OVER (PARTITION BY hash, node_kind) AS buy_owner
    FROM offers
),
offer_events AS (
    SELECT ledger_index, close_time, hash, tx_type, account,
           CASE WHEN node_kind = 'CreatedNode' THEN 'offer_create'
                WHEN tx_type = 'NFTokenAcceptOffer' AND NOT is_sell AND tx_has_sell THEN 'brokered_buy_leg'
                WHEN tx_type = 'NFTokenAcceptOffer' AND amt_value(f -> 'Amount') > 0 THEN 'sale'
                WHEN tx_type = 'NFTokenAcceptOffer' THEN 'transfer'
                WHEN tx_type = 'NFTokenCancelOffer' THEN 'offer_cancel'
                ELSE 'offer_removed' END AS event,
           f ->> 'NFTokenID' AS nftoken_id,
           f ->> 'Owner' AS owner,
           is_sell,
           CASE WHEN tx_type = 'NFTokenAcceptOffer' THEN CASE WHEN is_sell THEN f ->> 'Owner' ELSE account END END AS seller,
           CASE WHEN tx_type = 'NFTokenAcceptOffer' THEN
                CASE WHEN NOT is_sell THEN f ->> 'Owner' WHEN buy_owner IS NOT NULL THEN buy_owner ELSE account END END AS buyer,
           CASE WHEN tx_type = 'NFTokenAcceptOffer' AND tx_has_sell AND buy_owner IS NOT NULL THEN account END AS broker,
           amt_asset(f -> 'Amount') AS asset,
           amt_value(f -> 'Amount') AS value_f,
           amt_drops(f -> 'Amount') AS drops
    FROM offer_rows
)
-- NFTokenID layout: flags(2) fee(2) issuer(20) scrambled taxon(4) sequence(4) bytes.
-- The issuer's r-address needs base58 (a Python UDF, slow per row), so decode each distinct issuer once.
, e AS (
    SELECT ledger_index, close_time, hash, tx_type, account, event, nftoken_id,
           NULL::VARCHAR AS owner, NULL::BOOLEAN AS is_sell, NULL::VARCHAR AS seller, NULL::VARCHAR AS buyer,
           NULL::VARCHAR AS broker, NULL::VARCHAR AS asset, NULL::DOUBLE AS value_f, NULL::BIGINT AS drops
    FROM minted
    UNION ALL
    SELECT * FROM offer_events
),
issuers AS (
    SELECT issuer_hex, nft_issuer('00000000' || issuer_hex || '0000000000000000') AS issuer
    FROM (SELECT DISTINCT substr(nftoken_id, 9, 40) AS issuer_hex FROM e WHERE length(nftoken_id) = 64)
)
SELECT e.ledger_index, e.close_time, e.hash, e.tx_type, e.account, e.event, e.nftoken_id,
       i.issuer,
       xor(('0x' || substr(e.nftoken_id, 49, 8))::UBIGINT,
           (384160001 * ('0x' || substr(e.nftoken_id, 57, 8))::UBIGINT + 2459) % 4294967296) AS taxon,
       e.owner, e.is_sell, e.seller, e.buyer, e.broker, e.asset, e.value_f, e.drops
FROM e LEFT JOIN issuers i ON i.issuer_hex = substr(e.nftoken_id, 9, 40)
