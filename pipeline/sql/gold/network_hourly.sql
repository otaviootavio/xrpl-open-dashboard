-- Transactions and ledgers per UTC day and hour (the dashboard derives the weekday x hour grid).
SELECT close_time::DATE AS day, hour(close_time) AS hour_utc,
       sum(tx_count)::BIGINT AS txs, count(*) AS ledgers
FROM lg_src
WHERE close_time >= {day_from} AND close_time < {day_to} + 1
GROUP BY 1, 2
