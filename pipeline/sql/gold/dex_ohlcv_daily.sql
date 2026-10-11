-- Price and volume per UTC day and trading pair. A pair is (base, quote) with base = the smaller
-- asset key; price = quote units per base unit. Multi-hop payments count once per hop.
WITH s AS (
    SELECT close_time::DATE AS day, ledger_index, tx_index, hash, venue, taker,
           least(asset_out, asset_in) AS base, greatest(asset_out, asset_in) AS quote,
           CASE WHEN asset_out < asset_in THEN amount_out ELSE amount_in END AS base_amount,
           CASE WHEN asset_out < asset_in THEN amount_in ELSE amount_out END AS quote_amount
    FROM s_dex_swaps
    WHERE asset_out <> asset_in
)
, agg AS (
SELECT day, base, quote,
       arg_min(quote_amount / base_amount, (ledger_index, tx_index, quote_amount / base_amount)) AS open,
       max(quote_amount / base_amount)                   AS high,
       min(quote_amount / base_amount)                   AS low,
       arg_max(quote_amount / base_amount, (ledger_index, tx_index, -quote_amount / base_amount)) AS close,
       sum(base_amount ORDER BY base_amount) AS base_volume, sum(quote_amount ORDER BY quote_amount) AS quote_volume,
       count(*) AS trades,
       count(*) FILTER (WHERE venue = 'orderbook') AS ob_trades,
       count(*) FILTER (WHERE venue = 'amm') AS amm_trades,
       count(DISTINCT taker) AS takers
FROM s GROUP BY day, base, quote
)
-- readable codes are added after grouping (a macro over a grouped column trips a DuckDB binder bug)
SELECT day, base, quote,
       xrpl_currency_code(split_part(base, '.', 1)) AS base_code,
       xrpl_currency_code(split_part(quote, '.', 1)) AS quote_code,
       * EXCLUDE (day, base, quote)
FROM agg
