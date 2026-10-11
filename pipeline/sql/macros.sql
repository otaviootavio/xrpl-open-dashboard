-- XRPL Amount helpers. An Amount is a JSON string of drops (XRP) or an object
-- {"currency","issuer","value"} (issued token). Asset key: 'XRP' or '<currency>.<issuer>'.
CREATE OR REPLACE MACRO amt_is_xrp(j) AS json_type(j) = 'VARCHAR';
CREATE OR REPLACE MACRO amt_asset(j) AS
    CASE WHEN j IS NULL THEN NULL
         WHEN json_type(j) = 'VARCHAR' THEN 'XRP'
         ELSE (j ->> 'currency') || '.' || (j ->> 'issuer') END;
-- Value in natural units: XRP for XRP, token units for tokens.
CREATE OR REPLACE MACRO amt_value(j) AS
    CASE WHEN j IS NULL THEN NULL
         WHEN json_type(j) = 'VARCHAR' THEN (j ->> '$')::DOUBLE / 1e6
         ELSE (j ->> 'value')::DOUBLE END;
CREATE OR REPLACE MACRO amt_drops(j) AS
    CASE WHEN json_type(j) = 'VARCHAR' THEN (j ->> '$')::BIGINT END;
-- Asset key of an AMM pool's Asset/Asset2 field ({"currency":"XRP"} or {"currency","issuer"}).
CREATE OR REPLACE MACRO issue_key(j) AS
    CASE WHEN j IS NULL THEN NULL
         WHEN (j ->> 'currency') = 'XRP' AND (j ->> 'issuer') IS NULL THEN 'XRP'
         ELSE (j ->> 'currency') || '.' || (j ->> 'issuer') END;
-- Readable currency code: 3-letter codes pass through; 40-hex codes are decoded when they hold
-- printable ASCII ("524C5553440000..." -> "RLUSD"); AMM LP tokens (first byte 0x03) become 'LP'.
CREATE OR REPLACE MACRO xrpl_currency_code(cur) AS
    CASE WHEN cur IS NULL OR length(cur) <> 40 THEN cur
         WHEN starts_with(cur, '03') THEN 'LP'
         WHEN regexp_full_match(rtrim(try(decode(unhex(cur))), chr(0)), '[ -~]+')
              THEN rtrim(try(decode(unhex(cur))), chr(0))
         ELSE cur END;
