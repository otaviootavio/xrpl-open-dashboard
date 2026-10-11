"""Build report/dashboard.html: the 21 "low-hanging-fruit" analyses from the
project's analysis roadmap, run against the full Parquet export and rendered
as a self-contained HTML page.

    uv run report/build_dashboard.py [--data /local/data/xrpl] [--out report/dashboard.html]

Unlike build_export_report.py (which scopes to the newest ~24h to stay cheap), this
is a whole-dataset report. By default it scans transactions/*.parquet (341 GB) a
handful of times, which took 2 h 37 min on 2026-10-05. With --from-gold it reads
the gold tier built by pipeline/run.py instead and finishes in seconds.
"""

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import duckdb

HERE = Path(__file__).parent
XRPLD_RPC = "http://127.0.0.1:5005"

# XRPL mainnet, whole-network reference figures -- NOT re-fetched on every build.
# Captured from xrpscan.com / xrpl.org during this project's analysis session.
# Keep "as_of" accurate if these numbers are ever refreshed by hand.
XRPL_WIDE_BENCHMARK = {
    "as_of": "2026-10-04",
    "all_time_txs": "billions (14+ years of continuous history)",
    "daily_txs": 193_955,
    "funded_accounts": "8M+",
    "burn_per_day": 25.25,
    "network_age": "14+ years",
}


def currency(code: str | None) -> str | None:
    """Decode 40-hex-char currency codes to text when they are printable."""
    if code and len(code) == 40:
        try:
            s = bytes.fromhex(code).rstrip(b"\0").decode("ascii")
            if s.isprintable() and s:
                return s
        except (ValueError, UnicodeDecodeError):
            pass
        return code[:8] + "…"
    return code


def validator_status() -> dict:
    """One cheap RPC call to the local node, used only to show how far behind
    the live chain this dataset currently is. AGENTS.md: one `ledger` request
    per check is fine; bulk historical fetching from the local node is not."""
    try:
        req = urllib.request.Request(
            XRPLD_RPC,
            data=json.dumps({"method": "server_info", "params": [{}]}).encode(),
            headers={"Content-Type": "application/json"},
        )
        info = json.load(urllib.request.urlopen(req, timeout=5))["result"]["info"]
        return {"reachable": True, "validated_ledger": info["validated_ledger"]["seq"]}
    except Exception:
        return {"reachable": False, "validated_ledger": None}


def run_verify(data_dir: Path) -> dict:
    """Run the project's own verify_export.py and parse its OK/FAIL lines into
    structured checks for the data-integrity panel. This is also what finally
    exercises that overdue housekeeping step."""
    script = HERE.parent / ".claude" / "skills" / "xrpl-ledger-export" / "scripts" / "verify_export.py"
    try:
        proc = subprocess.run(
            ["uv", "run", str(script), "--data", str(data_dir)],
            cwd=HERE.parent, capture_output=True, text=True, timeout=2400,
        )
        checks = []
        for line in proc.stdout.splitlines():
            m = re.match(r"(OK|FAIL)\s+(.+?)\s{2,}(.*)", line)
            if m:
                checks.append({"ok": m[1] == "OK", "name": m[2].strip(), "detail": m[3].strip()})
        # Surface stderr whenever the script didn't produce the expected OK/FAIL lines, so a
        # crash or argument error is visible on the dashboard instead of silently showing nothing.
        raw = proc.stdout if checks else (proc.stdout + proc.stderr).strip() or f"(exit code {proc.returncode}, no output)"
        return {"ok": proc.returncode == 0 and bool(checks), "checks": checks, "raw": raw}
    except Exception as e:  # missing script, timeout, etc. -- don't fail the whole build
        return {"ok": False, "checks": [], "raw": f"verify_export.py did not complete: {e}"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=Path("/local/data/xrpl"))
    ap.add_argument("--out", type=Path, default=HERE / "dashboard.html")
    ap.add_argument("--from-gold", action="store_true",
                    help="read the gold tier (seconds) instead of scanning bronze (hours); see report/dashboard_gold.py")
    args = ap.parse_args()

    if args.from_gold:
        sys.path.insert(0, str(HERE))
        import dashboard_gold
        print("reading gold tables ...", flush=True)
        data = dashboard_gold.build(args.data, XRPL_WIDE_BENCHMARK, validator_status(), currency)
        write_page(data, args.out)
        return

    duckdb.sql("SET TimeZone = 'UTC'")
    duckdb.sql("SET threads = 2; SET memory_limit = '4GB'")  # leave the CPU and RAM to the validator

    names = sorted(f.name for f in (args.data / "transactions").glob("*.parquet")
                   if (args.data / "ledgers" / f.name).exists())
    if not names:
        sys.exit(f"No complete chunks found under {args.data}")
    tx = "read_parquet([" + ",".join(f"'{args.data}/transactions/{n}'" for n in names) + "])"
    lg = "read_parquet([" + ",".join(f"'{args.data}/ledgers/{n}'" for n in names) + "])"

    rows = lambda sql: [dict(zip(r.columns, v)) for r in [duckdb.sql(sql)] for v in r.fetchall()]
    row1 = lambda sql: rows(sql)[0]

    print(f"{len(names)} chunks -> querying ledgers (cheap) ...", flush=True)

    # ---------------------------------------------------------------- ledgers
    rng = row1(f"""
        SELECT min(ledger_index) first_ledger, max(ledger_index) last_ledger, count(*) ledgers,
               strftime(min(close_time), '%Y-%m-%dT%H:%M:%SZ') t0, strftime(max(close_time), '%Y-%m-%dT%H:%M:%SZ') t1
        FROM {lg}""")

    defl = row1(f"""
        SELECT min_by(total_drops, ledger_index) supply_start, max_by(total_drops, ledger_index) supply_end,
               (min_by(total_drops, ledger_index) - max_by(total_drops, ledger_index)) AS burned_drops,
               date_diff('day', min(close_time), max(close_time)) AS days
        FROM {lg}""")
    defl["annualized_pct"] = (defl["burned_drops"] / max(defl["supply_start"], 1)) * (365.0 / max(defl["days"], 1)) * 100

    busiest = row1(f"SELECT ledger_index, tx_count, strftime(close_time,'%Y-%m-%dT%H:%M:%SZ') close_time FROM {lg} ORDER BY tx_count DESC LIMIT 1")
    quietest = row1(f"SELECT ledger_index, tx_count, strftime(close_time,'%Y-%m-%dT%H:%M:%SZ') close_time FROM {lg} ORDER BY tx_count ASC LIMIT 1")

    latency = row1(f"""
        WITH d AS (
          SELECT ledger_index, close_time,
                 date_diff('second', lag(close_time) OVER (ORDER BY ledger_index), close_time) AS gap_s
          FROM {lg})
        SELECT avg(gap_s) avg_s, approx_quantile(gap_s, 0.95) p95_s, max(gap_s) max_s,
               arg_max(ledger_index, gap_s) max_at_ledger,
               strftime(arg_max(close_time, gap_s), '%Y-%m-%dT%H:%M:%SZ') max_at_time
        FROM d WHERE gap_s IS NOT NULL""")

    season_rows = rows(f"""
        SELECT extract(dow FROM close_time)::INT wd, extract(hour FROM close_time)::INT hr, sum(tx_count) n
        FROM {lg} GROUP BY 1, 2""")
    grid = [[0] * 24 for _ in range(7)]
    for r in season_rows:
        grid[r["wd"]][r["hr"]] = r["n"]

    print("querying transactions: single-row aggregate pass ...", flush=True)

    # ------------------------------------------------------- transactions: scan 1
    agg = row1(f"""
        SELECT
          count(*) AS total_txs,
          approx_count_distinct(account) AS distinct_senders,
          sum(fee_drops) AS fee_drops_total,
          count(*) FILTER (WHERE result = 'tesSUCCESS') AS ok_txs,
          count(*) FILTER (WHERE tx_type = 'OfferCreate') AS offer_creates,
          count(*) FILTER (WHERE tx_type = 'OfferCancel') AS offer_cancels,
          count(*) FILTER (WHERE result = 'tecPATH_DRY') AS tec_path_dry,
          count(*) FILTER (WHERE result = 'tecUNFUNDED_OFFER') AS tec_unfunded_offer,
          count(*) FILTER (WHERE tx_type = 'NFTokenMint') AS nft_mints,
          count(*) FILTER (WHERE tx_type = 'NFTokenBurn') AS nft_burns,
          max(fee_drops) AS max_fee_drops,
          arg_max(hash, fee_drops) AS max_fee_hash,
          arg_max(account, fee_drops) AS max_fee_account,
          arg_max(ledger_index, fee_drops) AS max_fee_ledger,
          strftime(arg_max(close_time, fee_drops), '%Y-%m-%dT%H:%M:%SZ') AS max_fee_time,
          max(delivered_drops) FILTER (WHERE tx_type = 'Payment') AS max_payment_drops,
          arg_max(account, delivered_drops) FILTER (WHERE tx_type = 'Payment') AS max_payment_account,
          arg_max(destination, delivered_drops) FILTER (WHERE tx_type = 'Payment') AS max_payment_destination,
          arg_max(ledger_index, delivered_drops) FILTER (WHERE tx_type = 'Payment') AS max_payment_ledger,
          strftime(arg_max(close_time, delivered_drops) FILTER (WHERE tx_type = 'Payment'), '%Y-%m-%dT%H:%M:%SZ') AS max_payment_time,
          count(*) FILTER (WHERE tx_type = 'Payment' AND delivered_drops = 1) AS one_drop_payments,
          min(delivered_drops) FILTER (WHERE tx_type = 'Payment' AND delivered_drops > 0) AS min_payment_drops,
          arg_min(account, delivered_drops) FILTER (WHERE tx_type = 'Payment' AND delivered_drops > 0) AS min_payment_account,
          count(*) FILTER (WHERE tx_type = 'Payment' AND delivered_drops > 0 AND delivered_drops < 1000000) AS b_lt1,
          count(*) FILTER (WHERE tx_type = 'Payment' AND delivered_drops >= 1000000 AND delivered_drops < 10000000) AS b_1_10,
          count(*) FILTER (WHERE tx_type = 'Payment' AND delivered_drops >= 10000000 AND delivered_drops < 100000000) AS b_10_100,
          count(*) FILTER (WHERE tx_type = 'Payment' AND delivered_drops >= 100000000 AND delivered_drops < 1000000000) AS b_100_1k,
          count(*) FILTER (WHERE tx_type = 'Payment' AND delivered_drops >= 1000000000 AND delivered_drops < 10000000000) AS b_1k_10k,
          count(*) FILTER (WHERE tx_type = 'Payment' AND delivered_drops >= 10000000000 AND delivered_drops < 100000000000) AS b_10k_100k,
          count(*) FILTER (WHERE tx_type = 'Payment' AND delivered_drops >= 100000000000 AND delivered_drops < 1000000000000) AS b_100k_1m,
          count(*) FILTER (WHERE tx_type = 'Payment' AND delivered_drops >= 1000000000000) AS b_gte1m
        FROM {tx}""")

    print("querying transactions: monthly rollup pass ...", flush=True)

    # ------------------------------------------------------- transactions: scan 2
    monthly = rows(f"""
        SELECT strftime(date_trunc('month', close_time), '%Y-%m') AS month,
               count(*) txs, count(*) FILTER (WHERE result = 'tesSUCCESS') ok,
               approx_quantile(fee_drops, 0.5) fee_p50, approx_quantile(fee_drops, 0.9) fee_p90, approx_quantile(fee_drops, 0.99) fee_p99,
               count(*) FILTER (WHERE tx_type = 'OfferCreate') offer_create, count(*) FILTER (WHERE tx_type = 'OfferCancel') offer_cancel,
               count(*) FILTER (WHERE tx_type = 'NFTokenMint') nft_mint, count(*) FILTER (WHERE tx_type = 'NFTokenBurn') nft_burn
        FROM {tx} GROUP BY 1 ORDER BY 1""")

    print("querying transactions: per-account rollup pass (the big one -- GROUP BY account) ...", flush=True)

    # ------------------------------------------------------- transactions: scan 3
    duckdb.sql(f"""
        CREATE OR REPLACE TEMP TABLE account_agg AS
        SELECT account, count(*) txs, count(DISTINCT destination_tag) distinct_tags,
               sum(delivered_drops) FILTER (WHERE tx_type = 'Payment' AND result = 'tesSUCCESS') sent_drops,
               min(close_time) first_seen
        FROM {tx} GROUP BY account""")

    dest_tags = rows("SELECT account, distinct_tags, txs FROM account_agg WHERE distinct_tags > 0 ORDER BY distinct_tags DESC LIMIT 10")

    conc = row1("""
        WITH pos AS (SELECT sent_drops FROM account_agg WHERE sent_drops > 0),
             tot AS (SELECT sum(sent_drops) t, count(*) n FROM pos),
             ranked AS (SELECT sent_drops, row_number() OVER (ORDER BY sent_drops) rn FROM pos)
        SELECT tot.n AS accounts, tot.t AS total_drops,
               (2.0 * sum(ranked.rn * ranked.sent_drops) / (tot.n * tot.t)) - (tot.n + 1.0) / tot.n AS gini,
               sum(pow(ranked.sent_drops * 1.0 / tot.t, 2)) AS hhi
        FROM ranked, tot GROUP BY tot.n, tot.t""")

    growth = rows("""
        SELECT strftime(date_trunc('week', first_seen), '%Y-%m-%d') AS week, count(*) new_accounts
        FROM account_agg GROUP BY 1 ORDER BY 1""")

    print("querying transactions: spam/burst pass (GROUP BY account, day) ...", flush=True)

    # ------------------------------------------------------- transactions: scan 4
    bursts = rows(f"""
        SELECT account, strftime(date_trunc('day', close_time), '%Y-%m-%d') AS day, count(*) tec_count
        FROM {tx} WHERE result LIKE 'tec%' GROUP BY 1, 2 HAVING count(*) >= 20 ORDER BY tec_count DESC LIMIT 20""")

    print("querying transactions: issued-currency pass ...", flush=True)

    # ------------------------------------------------------- transactions: scan 5
    iou = [
        {**r, "currency": currency(r["currency"])}
        for r in rows(f"""
            SELECT delivered_currency AS currency, delivered_issuer AS issuer, count(*) n
            FROM {tx} WHERE tx_type = 'Payment' AND result = 'tesSUCCESS' AND delivered_currency IS NOT NULL
            GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 10""")
    ]

    print("querying transactions: new-vs-returning pass (join against account_agg) ...", flush=True)

    # ------------------------------------------------------- transactions: scan 6
    new_vs_returning = rows(f"""
        SELECT strftime(date_trunc('month', t.close_time), '%Y-%m') AS month,
               count(*) FILTER (WHERE date_trunc('month', t.close_time) = date_trunc('month', a.first_seen)) new_txs,
               count(*) total_txs
        FROM {tx} t JOIN account_agg a USING (account)
        GROUP BY 1 ORDER BY 1""")

    rng["txs"] = agg["total_txs"]

    print("running verify_export.py (data-integrity panel) ...", flush=True)
    integrity = run_verify(args.data)

    print("checking local validator (one cheap request) ...", flush=True)
    val = validator_status()
    gap = (val["validated_ledger"] - rng["last_ledger"]) if val["reachable"] else None

    data = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data_range": rng,
        "validator": {
            "reachable": val["reachable"], "validated_ledger": val["validated_ledger"],
            "gap_ledgers": gap, "gap_days_est": (gap * 3.9 / 86400) if gap else None,
        },
        "macro": {
            "deflation": {**defl, "fee_drops": agg["fee_drops_total"]},
            "fee_trend": {
                "months": [m["month"] for m in monthly],
                "p50": [m["fee_p50"] for m in monthly], "p90": [m["fee_p90"] for m in monthly], "p99": [m["fee_p99"] for m in monthly],
            },
            "latency": latency,
            "success_trend": {
                "months": [m["month"] for m in monthly],
                "ok_pct": [100 * m["ok"] / m["txs"] if m["txs"] else 0 for m in monthly],
            },
            "busiest_ledger": busiest, "quietest_ledger": quietest,
            "seasonality": {"grid": grid},
            "account_growth": {"weeks": [g["week"] for g in growth], "new_accounts": [g["new_accounts"] for g in growth]},
            "account_totals": {"distinct_senders": agg["distinct_senders"]},
            "record_fee": {
                "fee_drops": agg["max_fee_drops"], "account": agg["max_fee_account"], "hash": agg["max_fee_hash"],
                "ledger_index": agg["max_fee_ledger"], "close_time": agg["max_fee_time"],
            },
            "biggest_payment": {
                "delivered_drops": agg["max_payment_drops"], "account": agg["max_payment_account"],
                "destination": agg["max_payment_destination"], "ledger_index": agg["max_payment_ledger"], "close_time": agg["max_payment_time"],
            },
            "curiosity": {
                "one_drop_payments": agg["one_drop_payments"], "min_payment_drops": agg["min_payment_drops"],
                "min_payment_account": agg["min_payment_account"],
            },
            "benchmark": {"as_of": XRPL_WIDE_BENCHMARK["as_of"], "xrpl_wide": XRPL_WIDE_BENCHMARK},
        },
        "dex": {
            "offer_churn": {
                "create": agg["offer_creates"], "cancel": agg["offer_cancels"],
                "ratio": agg["offer_cancels"] / agg["offer_creates"] if agg["offer_creates"] else 0,
                "months": [m["month"] for m in monthly],
                "create_by_month": [m["offer_create"] for m in monthly], "cancel_by_month": [m["offer_cancel"] for m in monthly],
            },
            "liquidity_stress": {
                "tec_path_dry": agg["tec_path_dry"], "tec_unfunded_offer": agg["tec_unfunded_offer"],
                "pct_of_all": 100 * (agg["tec_path_dry"] + agg["tec_unfunded_offer"]) / agg["total_txs"] if agg["total_txs"] else 0,
            },
        },
        "payments": {
            "histogram": {
                "buckets": ["<1 XRP", "1-10", "10-100", "100-1k", "1k-10k", "10k-100k", "100k-1M", ">=1M"],
                "counts": [agg["b_lt1"], agg["b_1_10"], agg["b_10_100"], agg["b_100_1k"], agg["b_1k_10k"],
                           agg["b_10k_100k"], agg["b_100k_1m"], agg["b_gte1m"]],
            },
            "destination_tags": dest_tags,
            "issued_currency": iou,
            "concentration": {"gini": conc["gini"], "hhi": conc["hhi"], "accounts": conc["accounts"]},
            "new_vs_returning": {
                "months": [r["month"] for r in new_vs_returning],
                "new_pct": [100 * r["new_txs"] / r["total_txs"] if r["total_txs"] else 0 for r in new_vs_returning],
            },
        },
        "nft": {
            "months": [m["month"] for m in monthly],
            "mint": [m["nft_mint"] for m in monthly], "burn": [m["nft_burn"] for m in monthly],
            "total_mint": agg["nft_mints"], "total_burn": agg["nft_burns"],
        },
        "security": {"bursts": bursts},
        "integrity": integrity,
    }

    write_page(data, args.out)


def write_page(data: dict, out: Path) -> None:
    template = (HERE / "dashboard_template.html").read_text()
    html = template.replace("/*__DATA__*/null", json.dumps(data, default=str))
    # Atomic write, same reasoning as build_export_report.py: a watcher must never see a half-written file.
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(html)
    tmp.replace(out)
    print(f"wrote {out} ({data['data_range']['txs'] or 0:,} txs, {data['data_range']['ledgers'] or 0:,} ledgers)")


if __name__ == "__main__":
    main()
