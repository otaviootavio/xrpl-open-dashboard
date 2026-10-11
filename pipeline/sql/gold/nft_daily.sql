-- NFT activity per UTC day and issuer.
SELECT close_time::DATE AS day, issuer,
       count(*) FILTER (WHERE event = 'mint') AS mints,
       count(*) FILTER (WHERE event = 'burn') AS burns,
       count(*) FILTER (WHERE event = 'offer_create') AS offers,
       count(*) FILTER (WHERE event = 'sale') AS sales,
       count(*) FILTER (WHERE event = 'transfer') AS transfers,
       (sum(drops) FILTER (WHERE event = 'sale'))::BIGINT AS sale_volume_drops
FROM s_nft_events GROUP BY 1, 2
