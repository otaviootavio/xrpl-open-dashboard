"""Dashboard data from the gold tier (story E6-1): the same `data` dict build_dashboard.py used to
compute from 342 GB of bronze, now read from a few MB of gold tables in seconds.

Used by `build_dashboard.py --from-gold`. Each section degrades to empty values when its gold table
is missing (e.g. before the silver backfill has run), so the page still renders.

Differences from the bronze build (all deliberate):
- fee percentiles and close-gap p95 are exact (the bronze build used approx_quantile);
- distinct senders are exact (account_first_seen), not approx_count_distinct;
- destination-tag hubs count tags on the *receiving* account (the bronze build grouped by sender,
  contradicting its own description) and sum monthly distinct counts, an upper bound;
- record payments exclude self-payments (arbitrage loops), and NFT counts are successful mints/burns.
"""

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
ISO = "%Y-%m-%dT%H:%M:%SZ"
BUCKETS = ["<1 XRP", "1-10", "10-100", "100-1k", "1k-10k", "10k-100k", "100k-1M", ">=1M"]


class Gold:
    def __init__(self, data: Path):
        self.root = data / "gold"
        self.con = duckdb.connect()
        self.con.execute("SET TimeZone = 'UTC'; SET threads = 2; SET memory_limit = '4GB'")

    def src(self, table: str) -> str | None:
        """read_parquet(...) over a gold table, or None if it has no data yet."""
        whole = self.root / f"{table}.parquet"
        if whole.exists():
            return f"read_parquet('{whole}')"
        parts = sorted((self.root / table).glob("*.parquet"))
        return "read_parquet([" + ",".join(f"'{p}'" for p in parts) + "], union_by_name = true)" if parts else None

    def rows(self, sql: str) -> list[dict]:
        cur = self.con.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def row(self, sql: str) -> dict:
        r = self.rows(sql)
        return r[0] if r else {}


def run_checks(project: Path, data: Path) -> dict:
    """The pipeline's cross-tier checks, parsed for the integrity panel."""
    try:
        proc = subprocess.run(["uv", "run", "pipeline/run.py", "check", "--data", str(data)], cwd=project,
                              capture_output=True, text=True, timeout=1800)
        checks = []
        for line in proc.stdout.splitlines():
            m = re.match(r"(OK|FAIL)\s+(.+?)\s{2,}(.*)", line)
            if m:
                checks.append({"ok": m[1] == "OK", "name": m[2].strip(), "detail": m[3].strip()})
        raw = proc.stdout if checks else (proc.stdout + proc.stderr).strip() or f"(exit code {proc.returncode})"
        return {"ok": proc.returncode == 0 and bool(checks), "checks": checks, "raw": raw}
    except Exception as e:  # don't fail the whole build
        return {"ok": False, "checks": [], "raw": f"run.py check did not complete: {e}"}


def build(data: Path, benchmark: dict, validator: dict, currency) -> dict:
    g = Gold(data)
    nd, nh, gaps, nm = g.src("network_daily"), g.src("network_hourly"), g.src("close_gaps_daily"), g.src("network_monthly")
    rec, afs, am = g.src("records"), g.src("account_first_seen"), g.src("account_monthly")
    psd, dtm, tok, nft, anom = (g.src(t) for t in ("payment_size_daily", "dest_tag_monthly", "token_daily", "nft_daily", "anomalies_daily"))
    if not nd:
        raise SystemExit("gold/network_daily is empty: run `uv run pipeline/run.py update` first")

    rng = g.row(f"""SELECT min(first_ledger) first_ledger, max(last_ledger) last_ledger, sum(ledgers) ledgers,
                           strftime(min(first_close), '{ISO}') AS t0, strftime(max(last_close), '{ISO}') AS t1, sum(txs) AS txs
                    FROM {nd}""")
    defl = g.row(f"""SELECT arg_min(supply_first, day) supply_start, arg_max(supply_last, day) supply_end,
                            date_diff('day', min(first_close), max(last_close)) AS days, sum(fees_drops) fee_drops
                     FROM {nd}""")
    defl["burned_drops"] = (defl["supply_start"] or 0) - (defl["supply_end"] or 0)
    defl["annualized_pct"] = defl["burned_drops"] / max(defl["supply_start"] or 1, 1) * 365.0 / max(defl["days"] or 1, 1) * 100

    records = {r["record"]: r for r in g.rows(f"SELECT *, strftime(close_time, '{ISO}') AS t FROM {rec}")} if rec else {}
    r = lambda name: records.get(name, {})

    latency = {"avg_s": None, "p95_s": None, "max_s": r("longest_close_gap").get("value"),
               "max_at_ledger": r("longest_close_gap").get("ledger_index"), "max_at_time": r("longest_close_gap").get("t")}
    if gaps:
        latency.update(g.row(f"""
            WITH h AS (SELECT gap_s, sum(ledgers) n FROM {gaps} GROUP BY 1),
                 c AS (SELECT gap_s, n, sum(n) OVER (ORDER BY gap_s) cum, sum(n) OVER () tot FROM h)
            SELECT sum(gap_s * n) / sum(n) avg_s, min(gap_s) FILTER (WHERE cum >= 0.95 * tot) p95_s FROM c"""))

    grid = [[0] * 24 for _ in range(7)]
    if nh:
        for x in g.rows(f"SELECT dayofweek(day) wd, hour_utc hr, sum(txs) n FROM {nh} GROUP BY 1, 2"):
            grid[x["wd"]][x["hr"]] = int(x["n"])

    monthly = g.rows(f"""SELECT strftime(month, '%Y-%m') AS month, txs, txs_ok, fee_p50, fee_p90, fee_p99,
                                offer_creates, offer_cancels, tec_path_dry, tec_unfunded_offer
                         FROM {nm} ORDER BY 1""") if nm else []
    months = [m["month"] for m in monthly]
    tot = lambda k: sum(m[k] or 0 for m in monthly)

    growth = g.rows(f"""SELECT strftime(date_trunc('week', first_seen_time), '%Y-%m-%d') AS week, count(*) AS n
                        FROM {afs} WHERE first_seen_time IS NOT NULL GROUP BY 1 ORDER BY 1""") if afs else []
    senders = g.row(f"SELECT count(*) n FROM {afs} WHERE first_seen_ledger IS NOT NULL").get("n") if afs else None

    hist = {x["bucket"]: x for x in g.rows(f"SELECT bucket, sum(payments) n, sum(one_drop) one FROM {psd} GROUP BY 1")} if psd else {}

    conc = g.row(f"""
        WITH pos AS (SELECT account, sum(sent_drops) s FROM {am} GROUP BY 1 HAVING sum(sent_drops) > 0),
             tot AS (SELECT sum(s) t, count(*) n FROM pos),
             ranked AS (SELECT s, row_number() OVER (ORDER BY s) rn FROM pos)
        SELECT tot.n accounts, (2.0 * sum(rn * s) / (tot.n * tot.t)) - (tot.n + 1.0) / tot.n gini,
               sum(pow(s * 1.0 / tot.t, 2)) hhi
        FROM ranked, tot GROUP BY tot.n, tot.t""") if am else {}

    nvr = g.rows(f"""
        SELECT strftime(m.month, '%Y-%m') AS month,
               100.0 * sum(m.txs) FILTER (WHERE date_trunc('month', f.first_seen_time)::DATE = m.month) / sum(m.txs) new_pct
        FROM {am} m LEFT JOIN {afs} f USING (account) GROUP BY 1 ORDER BY 1""") if am and afs else []

    nft_m = g.rows(f"""SELECT strftime(date_trunc('month', day), '%Y-%m') AS month, sum(mints) mint, sum(burns) burn
                       FROM {nft} GROUP BY 1 ORDER BY 1""") if nft else []

    return {
        "generated_at": datetime.now(timezone.utc).strftime(ISO),
        "source": "gold",
        "data_range": rng,
        "validator": {
            "reachable": validator["reachable"], "validated_ledger": validator["validated_ledger"],
            "gap_ledgers": (validator["validated_ledger"] - rng["last_ledger"]) if validator["reachable"] else None,
            "gap_days_est": ((validator["validated_ledger"] - rng["last_ledger"]) * 3.9 / 86400) if validator["reachable"] else None,
        },
        "macro": {
            "deflation": defl,
            "fee_trend": {"months": months, "p50": [m["fee_p50"] for m in monthly],
                          "p90": [m["fee_p90"] for m in monthly], "p99": [m["fee_p99"] for m in monthly]},
            "latency": latency,
            "success_trend": {"months": months, "ok_pct": [100 * m["txs_ok"] / m["txs"] if m["txs"] else 0 for m in monthly]},
            "busiest_ledger": {"ledger_index": r("busiest_ledger").get("ledger_index"), "tx_count": r("busiest_ledger").get("value"),
                               "close_time": r("busiest_ledger").get("t")},
            "quietest_ledger": {"ledger_index": r("quietest_ledger").get("ledger_index"), "tx_count": r("quietest_ledger").get("value"),
                                "close_time": r("quietest_ledger").get("t")},
            "seasonality": {"grid": grid},
            "account_growth": {"weeks": [x["week"] for x in growth], "new_accounts": [x["n"] for x in growth]},
            "account_totals": {"distinct_senders": senders},
            "record_fee": {"fee_drops": r("record_fee").get("value"), "account": r("record_fee").get("account"),
                           "hash": r("record_fee").get("hash"), "ledger_index": r("record_fee").get("ledger_index"),
                           "close_time": r("record_fee").get("t")},
            "biggest_payment": {"delivered_drops": r("biggest_xrp_payment").get("value"), "account": r("biggest_xrp_payment").get("account"),
                                "destination": r("biggest_xrp_payment").get("counterparty"),
                                "ledger_index": r("biggest_xrp_payment").get("ledger_index"), "close_time": r("biggest_xrp_payment").get("t")},
            "curiosity": {"one_drop_payments": sum(int(x["one"] or 0) for x in hist.values()) if hist else None,
                          "min_payment_drops": r("smallest_xrp_payment").get("value"),
                          "min_payment_account": r("smallest_xrp_payment").get("account")},
            "benchmark": {"as_of": benchmark["as_of"], "xrpl_wide": benchmark},
        },
        "dex": {
            "offer_churn": {"create": tot("offer_creates"), "cancel": tot("offer_cancels"),
                            "ratio": tot("offer_cancels") / tot("offer_creates") if tot("offer_creates") else 0,
                            "months": months, "create_by_month": [m["offer_creates"] for m in monthly],
                            "cancel_by_month": [m["offer_cancels"] for m in monthly]},
            "liquidity_stress": {"tec_path_dry": tot("tec_path_dry"), "tec_unfunded_offer": tot("tec_unfunded_offer"),
                                 "pct_of_all": 100 * (tot("tec_path_dry") + tot("tec_unfunded_offer")) / tot("txs") if tot("txs") else 0},
        },
        "payments": {
            "histogram": {"buckets": BUCKETS, "counts": [int(hist[b]["n"]) if b in hist else 0 for b in BUCKETS]},
            "destination_tags": g.rows(f"""SELECT account, sum(distinct_tags) distinct_tags, sum(transfers) txs
                                           FROM {dtm} GROUP BY 1 ORDER BY 2 DESC LIMIT 10""") if dtm else [],
            "issued_currency": [{**x, "currency": x["currency"] or currency(x["asset"].split(".")[0])} for x in g.rows(f"""
                SELECT asset, any_value(currency_code) currency, any_value(issuer) issuer, sum(transfers) n
                FROM {tok} GROUP BY 1 ORDER BY 4 DESC LIMIT 10""")] if tok else [],
            "concentration": {"gini": conc.get("gini"), "hhi": conc.get("hhi"), "accounts": conc.get("accounts")},
            "new_vs_returning": {"months": [x["month"] for x in nvr], "new_pct": [x["new_pct"] for x in nvr]},
        },
        "nft": {"months": [x["month"] for x in nft_m], "mint": [x["mint"] for x in nft_m], "burn": [x["burn"] for x in nft_m],
                "total_mint": sum(x["mint"] for x in nft_m), "total_burn": sum(x["burn"] for x in nft_m)},
        "security": {"bursts": g.rows(f"""SELECT account, strftime(day, '%Y-%m-%d') AS day, tec_count FROM {anom}
                                          WHERE flood ORDER BY tec_count DESC, day LIMIT 20""") if anom else []},
        "integrity": run_checks(HERE.parent, data),
    }
