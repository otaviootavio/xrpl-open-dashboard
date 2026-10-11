-- Issued-token activity per UTC day and asset (<currency>.<issuer>).
WITH tr AS (
    SELECT close_time::DATE AS day, asset, any_value(currency_code) AS currency_code,
           count(*) AS transfers, sum(value_f ORDER BY value_f) AS volume,
           count(DISTINCT from_account) AS senders, count(DISTINCT to_account) AS receivers
    FROM s_transfers WHERE asset <> 'XRP' GROUP BY 1, 2
),
tl AS (
    SELECT close_time::DATE AS day, asset, any_value(currency_code) AS currency_code,
           count(*) FILTER (WHERE change = 'created') AS trustlines_created,
           count(*) FILTER (WHERE change = 'deleted') AS trustlines_deleted
    FROM s_trustline_changes WHERE currency_code <> 'LP' GROUP BY 1, 2
)
SELECT day, asset, coalesce(tr.currency_code, tl.currency_code) AS currency_code,
       split_part(asset, '.', 2) AS issuer,
       coalesce(transfers, 0) AS transfers, volume, coalesce(senders, 0) AS senders,
       coalesce(receivers, 0) AS receivers,
       coalesce(trustlines_created, 0) AS trustlines_created,
       coalesce(trustlines_deleted, 0) AS trustlines_deleted
FROM tr FULL JOIN tl USING (day, asset)
