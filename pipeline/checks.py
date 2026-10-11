"""Cross-tier checks for `run.py check`. Each prints `OK  <name>  <detail>` or `FAIL <name>  <detail>`,
in the same format as verify_export.py, so the dashboard's integrity panel can parse both."""

from datetime import date
from pathlib import Path

# Monthly figures from the 2026-10-05 dashboard build (report/dashboard.html), which scanned bronze
# directly. Only whole months whose bronze has not changed since are compared. Success % must match
# exactly. The dashboard's fee percentiles came from approx_quantile: p50/p90 may be off by one drop
# (2026-06 p90: exact 20, approx 21) and p99 is far off on this long-tailed column (2026-08: exact
# 1,000 drops, approx 1,677-1,920), so p50/p90 are compared within 1 drop and p99 is only reported.
DASHBOARD_2026_10_05 = {
    "2025-10": (11, 15, 6011, 82.29026569087081), "2025-11": (11, 15, 6268, 82.6625503235072),
    "2025-12": (11, 15, 5871, 83.08613555423177), "2026-01": (11, 25, 5474, 81.22079955792456),
    "2026-02": (12, 25, 5981, 83.37545531828425), "2026-03": (12, 25, 5330, 78.55527517870405),
    "2026-04": (12, 15, 3477, 79.30092588929199), "2026-05": (12, 25, 4005, 72.98116406121223),
    "2026-06": (12, 21, 4600, 72.69621310220543), "2026-07": (11, 20, 4146, 77.17122794728623),
    "2026-08": (11, 21, 1920, 83.19747959742027),
}


def run(con, p, args) -> bool:
    from tiers import SILVER

    results = []

    def check(name, ok, detail):
        results.append((name, bool(ok), detail))

    d0, d1 = None, None
    if args.days:
        a, b = args.days.split("..")
        d0, d1 = date.fromisoformat(a), date.fromisoformat(b)
    rng = "" if d0 is None else f" AND day >= DATE '{d0}' AND day <= DATE '{d1}'"
    def brng_for(alias="b"):
        return "" if d0 is None else f" AND {alias}.day_min <= DATE '{d1}' AND {alias}.day_max >= DATE '{d0}'"
    brng = brng_for()

    # 1. every bronze chunk processed for every silver table in use
    in_use = [t.name for t in SILVER if con.execute(
        "SELECT count(*) FROM silver_chunks WHERE table_name = ?", [t.name]).fetchone()[0]]
    missing = {}
    for t in in_use:
        n = con.execute(f"""SELECT count(*) FROM bronze_chunks b WHERE true {brng} AND NOT EXISTS
            (SELECT 1 FROM silver_chunks s WHERE s.chunk = b.chunk AND s.table_name = ?)""", [t]).fetchone()[0]
        if n:
            missing[t] = n
    total = con.execute(f"SELECT count(*) FROM bronze_chunks b WHERE true {brng}").fetchone()[0]
    check("state_complete", not missing,
          f"{total:,} bronze chunks; tables in use {len(in_use)}; unprocessed: {missing or 'none'}")

    # 2. transactions_base keeps every bronze row
    bad = con.execute(f"""SELECT count(*) FROM silver_chunks s JOIN bronze_chunks b USING (chunk)
        WHERE s.table_name = 'transactions_base' AND s.rows <> b.tx_rows {brng}""").fetchone()[0]
    check("silver_rows", bad == 0, f"{bad} chunks where transactions_base rows differ from bronze")

    nd = sorted((p.gold / "network_daily").glob("day=*.parquet"))
    if not nd:
        check("gold_present", False, "no network_daily parts in gold")
    else:
        nd_sql = "read_parquet([" + ",".join(f"'{f}'" for f in nd) + "])"
        # 3. gold transaction counts agree with the ledger headers
        days, bad_days = con.execute(f"""SELECT count(*), count(*) FILTER (WHERE txs <> ledger_txs)
            FROM {nd_sql} WHERE true {rng}""").fetchone()
        check("daily_txs", bad_days == 0, f"{days:,} days; {bad_days} where txs <> sum(ledger.tx_count)")

        # 4. XRP burned = fees paid, per day (the dataset's first day has no predecessor ledger).
        # Exception: a tecINSUFF_FEE transaction burns only what its account holds, so on days with
        # one the burn may be lower than the declared fees, never higher.
        first = con.execute(f"SELECT min(day) FROM {nd_sql}").fetchone()[0]
        n, bad_burn, short, burned, fees = con.execute(f"""SELECT count(*),
            count(*) FILTER (WHERE burned_drops > fees_drops OR (burned_drops < fees_drops AND coalesce(insuff_fee_txs, 0) = 0)),
            count(*) FILTER (WHERE burned_drops < fees_drops AND insuff_fee_txs > 0),
            sum(burned_drops), sum(fees_drops) FROM {nd_sql} WHERE day > DATE '{first}' {rng}""").fetchone()
        check("burn_vs_fees", bad_burn == 0,
              f"{n:,} days; {bad_burn} unexplained; {short} short by tecINSUFF_FEE; "
              f"burned {(burned or 0) / 1e6:,.6f} XRP vs fees {(fees or 0) / 1e6:,.6f} XRP")

        # 5. no missing days between the first and last day in gold
        lo, hi, cnt = con.execute(f"SELECT min(day), max(day), count(DISTINCT day) FROM {nd_sql}").fetchone()
        check("gold_complete", cnt == (hi - lo).days + 1, f"network_daily {lo}..{hi}: {cnt} of {(hi - lo).days + 1} days")

    # 6. swaps have positive amounts on both legs
    sw = [r[0] for r in con.execute("SELECT out_path FROM silver_chunks WHERE table_name = 'dex_swaps'").fetchall()]
    if sw:
        n, bad = con.execute(f"""SELECT count(*), count(*) FILTER (WHERE NOT (amount_out > 0 AND amount_in > 0))
            FROM read_parquet([{",".join(f"'{f}'" for f in sorted(set(sw)))}])""").fetchone()
        check("swap_sanity", bad == 0, f"{n:,} swaps; {bad} with a non-positive leg")

    # 7. regression against the 2026-10-05 dashboard (built straight from bronze)
    nm = sorted((p.gold / "network_monthly").glob("month=*.parquet"))
    if nm:
        rows = con.execute("SELECT strftime(month, '%Y-%m'), fee_p50, fee_p90, fee_p99, success_pct FROM read_parquet(["
                           + ",".join(f"'{f}'" for f in nm) + "])").fetchall()
        compared, diffs, diffs_p99 = 0, [], []
        for m, p50, p90, p99, ok in rows:
            ref = DASHBOARD_2026_10_05.get(m)
            if not ref:
                continue
            compared += 1
            if abs(p50 - ref[0]) > 1 or abs(p90 - ref[1]) > 1 or abs(ok - ref[3]) > 1e-9:
                diffs.append(f"{m}: gold p50/p90/ok% {p50}/{p90}/{ok:.6f} vs {ref[0]}/{ref[1]}/{ref[3]:.6f}")
            else:
                diffs_p99.append(f"{m} p99 {p99} (dashboard approx {ref[2]})")
        check("dashboard_regression", not diffs, f"{compared} months compared; "
              + ("; ".join(diffs) or "success % exact, p50/p90 within 1 drop; " + ", ".join(diffs_p99)))

    # 8. the transfer graph's edges carry every non-self transfer, per month and asset
    te = sorted((p.gold / "transfer_edges_monthly").glob("month=*.parquet"))
    tr = sorted({r[0] for r in con.execute(
        "SELECT out_path FROM silver_chunks WHERE table_name = 'transfers'").fetchall()})
    if d0 is not None:
        lo, hi = f"month={d0:%Y-%m}.parquet", f"month={d1:%Y-%m}.parquet"
        te = [f for f in te if lo <= f.name <= hi]
    if te and tr:
        con.execute(f"""CREATE OR REPLACE TEMP TABLE chk_edges AS
            SELECT month, asset, sum(n_transfers) AS n FROM read_parquet([{",".join(f"'{f}'" for f in te)}])
            GROUP BY ALL""")
        bad = con.execute(f"""WITH s AS (
                SELECT date_trunc('month', close_time)::DATE AS month, coalesce(asset, 'MPT') AS asset, count(*) AS n
                FROM read_parquet([{",".join(f"'{f}'" for f in tr)}])
                WHERE NOT is_self AND date_trunc('month', close_time)::DATE IN (SELECT DISTINCT month FROM chk_edges)
                GROUP BY ALL)
            SELECT strftime(month, '%Y-%m') || ' ' || asset[:12] || ': edges ' || coalesce(e.n, 0) || ' vs transfers ' || coalesce(s.n, 0)
            FROM chk_edges e FULL JOIN s USING (month, asset)
            WHERE e.n IS DISTINCT FROM s.n ORDER BY 1""").fetchall()
        months = con.execute("SELECT count(DISTINCT month), sum(n) FROM chk_edges").fetchone()
        check("edges_match_transfers", not bad, f"{months[0]} months, {months[1]:,} transfers; "
              + (f"{len(bad)} mismatches: " + "; ".join(r[0] for r in bad[:3]) if bad else "every month and asset matches"))

    width = max(len(r[0]) for r in results)
    for name, ok, detail in results:
        print(f"{'OK  ' if ok else 'FAIL'} {name:<{width}}  {detail}")
    return all(ok for _, ok, _ in results)
