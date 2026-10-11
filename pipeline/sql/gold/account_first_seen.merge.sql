-- Merge candidates into the existing table; the earliest sighting wins, so the order chunks are processed in does not matter.
SELECT account,
       min(first_seen_ledger) AS first_seen_ledger,
       min(first_seen_time)   AS first_seen_time,
       min(created_ledger)    AS created_ledger,
       arg_min(funded_by, created_ledger) AS funded_by
FROM (SELECT * FROM g_existing UNION ALL BY NAME SELECT * FROM cand)
GROUP BY account
