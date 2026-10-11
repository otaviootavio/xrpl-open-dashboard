-- Ledger close gaps per UTC day: how many ledgers closed n seconds after their predecessor.
-- Gaps are whole seconds, so this small table gives exact percentiles over any range.
WITH lg AS (
    SELECT close_time, epoch(close_time) - epoch(lag(close_time) OVER (ORDER BY ledger_index)) AS gap_s
    FROM lg_src
)
SELECT close_time::DATE AS day, gap_s::INT AS gap_s, count(*) AS ledgers
FROM lg
WHERE gap_s IS NOT NULL AND close_time >= {day_from} AND close_time < {day_to} + 1
GROUP BY 1, 2
