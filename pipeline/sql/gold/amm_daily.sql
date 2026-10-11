-- AMM pool activity per UTC day and pool.
WITH swaps AS (
    SELECT close_time::DATE AS day, maker AS pool, any_value(amm_id) AS amm_id,
           count(*) AS swaps, count(DISTINCT taker) AS swappers,
           sum(xrp_leg ORDER BY xrp_leg) AS xrp_volume
    FROM (SELECT *, CASE WHEN asset_in = 'XRP' THEN amount_in WHEN asset_out = 'XRP' THEN amount_out END AS xrp_leg
          FROM s_dex_swaps WHERE venue = 'amm')
    GROUP BY 1, 2
),
ev AS (
    SELECT close_time::DATE AS day, pool, any_value(amm_id) AS amm_id,
           count(*) FILTER (WHERE event = 'deposit')  AS deposits,
           count(*) FILTER (WHERE event = 'withdraw') AS withdrawals,
           count(*) FILTER (WHERE event = 'bid')      AS bids,
           count(*) FILTER (WHERE event = 'vote')     AS votes,
           count(*) FILTER (WHERE event = 'create')   AS creates,
           count(DISTINCT account) AS lp_users,
           sum(lp_delta ORDER BY lp_delta) AS lp_tokens_delta
    FROM s_amm_events WHERE pool IS NOT NULL GROUP BY 1, 2
)
SELECT day, pool, coalesce(swaps.amm_id, ev.amm_id) AS amm_id,
       coalesce(swaps, 0) AS swaps, coalesce(swappers, 0) AS swappers, xrp_volume,
       coalesce(deposits, 0) AS deposits, coalesce(withdrawals, 0) AS withdrawals,
       coalesce(bids, 0) AS bids, coalesce(votes, 0) AS votes, coalesce(creates, 0) AS creates,
       coalesce(lp_users, 0) AS lp_users, lp_tokens_delta
FROM swaps FULL JOIN ev USING (day, pool)
