-- Native-XRP payment sizes per UTC day, in decade buckets (whale vs retail), plus exact-1-drop payments.
SELECT close_time::DATE AS day,
       CASE WHEN drops < 1000000 THEN '<1 XRP'
            WHEN drops < 10000000 THEN '1-10'
            WHEN drops < 100000000 THEN '10-100'
            WHEN drops < 1000000000 THEN '100-1k'
            WHEN drops < 10000000000 THEN '1k-10k'
            WHEN drops < 100000000000 THEN '10k-100k'
            WHEN drops < 1000000000000 THEN '100k-1M'
            ELSE '>=1M' END AS bucket,
       count(*) AS payments,
       count(*) FILTER (WHERE drops = 1) AS one_drop,
       sum(drops)::BIGINT AS drops
FROM s_transfers
WHERE asset = 'XRP' AND tx_type = 'Payment' AND drops > 0
GROUP BY 1, 2
