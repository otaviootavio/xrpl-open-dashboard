-- Candidates from newly processed chunks: first time an account sent a transaction, and when it was created.
WITH sent AS (
    SELECT account, min(ledger_index) AS first_seen_ledger, min(close_time) AS first_seen_time
    FROM s_transactions_base GROUP BY 1
),
made AS (
    SELECT account, min(ledger_index) AS created_ledger, arg_min(funded_by, ledger_index) AS funded_by
    FROM s_account_lifecycle WHERE event = 'created' GROUP BY 1
)
SELECT coalesce(sent.account, made.account) AS account,
       first_seen_ledger, first_seen_time, created_ledger, funded_by
FROM sent FULL JOIN made ON sent.account = made.account
