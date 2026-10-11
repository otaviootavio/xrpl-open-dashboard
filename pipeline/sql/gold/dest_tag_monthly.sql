-- Receiving accounts that see many distinct destination tags in a month: exchange-style deposit hubs.
-- Kept to accounts with at least 100 distinct tags in the month so the table stays small.
SELECT date_trunc('month', close_time)::DATE AS month, to_account AS account,
       count(DISTINCT destination_tag) AS distinct_tags, count(*) AS transfers
FROM s_transfers
WHERE destination_tag IS NOT NULL
GROUP BY 1, 2
HAVING count(DISTINCT destination_tag) >= 100
