"""Build a self-contained HTML report of the Parquet export in /local/data/xrpl.

    uv run report/build_export_report.py [--data /local/data/xrpl] [--log export_backfill.log] [--out report/export.html]

Reads progress from the newest exporter log (so it also works mid-run) and aggregates the
Parquet files with DuckDB. Transaction statistics cover only the most recent ~24 h on disk, so the
hourly build stays cheap as the dataset grows; whole-dataset totals come from the small ledger files.
The data is inlined, so the HTML opens anywhere.
"""

import argparse
import json
import os
import re
import socket
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import duckdb

HERE = Path(__file__).parent
DISKS = [  # (mount, label, media)
    ("/local/data", "Research data", "RAID1 · 2× 20 TB HDD"),
    ("/validatorfs", "xrpld database", "NVMe SSD"),
    ("/", "System", "HDD"),
]
XRPLD_RPC = "http://127.0.0.1:5005"
RECENT_LEDGERS = 22_500  # ~24 h of ledgers at ~3.9 s each
REFRESH_MINUTE = 9       # built by cron at :05, published by Claude at :09 each hour; shown as "next update"
# The project goal shown at the top of the report. The 12-month window ends where the first 24 h export starts.
GOAL = {
    "title": "Twelve months of XRP Ledger mainnet history, open to researchers",
    "why": "Every ledger and transaction, with its raw metadata, as Parquet files anyone can query with DuckDB, pandas or R.",
    "from": "2025-09-28T00:00:00Z",
    "to": "2026-09-27T00:56:40Z",
    "milestones": [
        {"name": "Last 24 hours", "detail": "22,159 ledgers · 3.0M transactions", "status": "done"},
        {"name": "Last 12 months", "detail": "8.1M ledgers · ~500 GB", "status": "backfill"},
        {"name": "Full chain since 2013", "detail": "~107M ledgers · ~2.5 TB", "status": "planned"},
    ],
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


def cpu_times() -> tuple[int, int]:
    f = [int(x) for x in Path("/proc/stat").read_text().split("\n")[0].split()[1:]]
    return f[3] + f[4], sum(f)  # (idle + iowait, total)


def system_info() -> dict:
    """Host, CPU, memory, disk and xrpld status, read from /proc, statvfs and the local node."""
    idle0, tot0 = cpu_times()
    time.sleep(1)
    idle1, tot1 = cpu_times()
    mem = {k: int(v.split()[0]) * 1024 for k, v in
           (l.split(":", 1) for l in Path("/proc/meminfo").read_text().splitlines())}
    cpuinfo = Path("/proc/cpuinfo").read_text()
    model = re.search(r"model name\s*:\s*(.+)", cpuinfo)
    disks = []
    for mount, label, media in DISKS:
        try:
            st = os.statvfs(mount)
        except OSError:
            continue
        disks.append({"mount": mount, "label": label, "media": media,
                      "total": st.f_blocks * st.f_frsize, "free": st.f_bavail * st.f_frsize})

    def procs(pattern: str) -> list[dict]:
        out = subprocess.run(["ps", "-eo", "pcpu,rss,etimes,args", "--no-headers"], capture_output=True, text=True).stdout
        return [{"cpu": float(c), "rss": int(r) * 1024, "elapsed_s": int(e)}
                for c, r, e, *a in (l.split(None, 3) for l in out.splitlines())
                if re.search(pattern, a[0] if a else "")]

    node = {"reachable": False}
    try:
        req = urllib.request.Request(XRPLD_RPC, data=json.dumps({"method": "server_info", "params": [{}]}).encode(),
                                     headers={"Content-Type": "application/json"})
        info = json.load(urllib.request.urlopen(req, timeout=5))["result"]["info"]
        node = {"reachable": True, "state": info.get("server_state"), "peers": info.get("peers"),
                "version": info.get("build_version"), "uptime_s": info.get("uptime"),
                "validated": (info.get("validated_ledger") or {}).get("seq"),
                "complete_ledgers": info.get("complete_ledgers"),
                "amendment_blocked": bool(info.get("amendment_blocked")),
                "warnings": [w.get("message") for w in info.get("warnings", [])]}
    except Exception as e:  # node down or restarting: report it rather than fail the page
        node["error"] = str(e)
    xp = procs(r"^/usr/bin/xrpld|/xrpld ")
    if xp:
        node["process"] = xp[0]
    return {
        "host": socket.gethostname(),
        "kernel": os.uname().release,
        "uptime_s": int(float(Path("/proc/uptime").read_text().split()[0])),
        "cpu_model": model[1].strip() if model else None,
        "cores": os.cpu_count(),
        "cpu_pct": round(100 * (1 - (idle1 - idle0) / max(tot1 - tot0, 1)), 1),
        "load": [float(x) for x in Path("/proc/loadavg").read_text().split()[:3]],
        "mem_total": mem["MemTotal"], "mem_available": mem["MemAvailable"],
        "disks": disks,
        "node": node,
        "exporter_running": bool(procs(r"export_ledgers\.py")),
    }


def parse_log(path: Path) -> dict:
    p = {"state": "unknown"}
    if not path.exists():
        return p
    text = path.read_text()
    text = text[text.rfind("Range "):] if "Range " in text else text  # only the latest run counts
    if m := re.search(r"Range ([\d,]+) \.\. ([\d,]+) \(([\d,]+) ledgers\)", text):
        p.update(start=int(m[1].replace(",", "")), end=int(m[2].replace(",", "")),
                 total=int(m[3].replace(",", "")), state="running")
    rows = re.findall(r"\| ([\d,]+)/([\d,]+) ledgers\s+([\d.]+)/s\s+retries (\d+)\s+ETA (\d+) min", text)
    if rows:
        done, _, rate, retries, eta = rows[-1]
        p.update(done=int(done.replace(",", "")), rate=float(rate), retries=int(retries), eta_min=int(eta))
    if "Done:" in text:
        p["state"] = "complete"
    elif re.search(r"Traceback|Interrupted", text):
        p["state"] = "stopped"
    return p


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=Path("/local/data/xrpl"))
    ap.add_argument("--log", help="exporter log, relative to --data (default: newest export_*.log)")
    ap.add_argument("--out", type=Path, default=HERE / "export.html")
    args = ap.parse_args()

    duckdb.sql("SET TimeZone = 'UTC'")  # report every timestamp in UTC
    duckdb.sql("SET threads = 2; SET memory_limit = '4GB'")  # leave the CPU and RAM to the validator
    # Freeze the chunk list up front: the exporter may write new files while we query.
    names = sorted(f.name for f in (args.data / "transactions").glob("*.parquet")
                   if (args.data / "ledgers" / f.name).exists())
    last = max(int(n.split("-")[1].split(".")[0]) for n in names)
    all_names, names = names, [n for n in names if int(n.split("-")[1].split(".")[0]) > last - RECENT_LEDGERS]
    tx_files = "[" + ",".join(f"'{args.data}/transactions/{n}'" for n in names) + "]"
    tx = f"read_parquet({tx_files})"
    lg = "read_parquet([" + ",".join(f"'{args.data}/ledgers/{n}'" for n in names) + "])"
    lg_all = "read_parquet([" + ",".join(f"'{args.data}/ledgers/{n}'" for n in all_names) + "])"
    logs = sorted(args.data.glob("export_*.log"), key=lambda f: f.stat().st_mtime)
    log = args.data / args.log if args.log else (logs[-1] if logs else args.data / "export.log")
    q = lambda sql: duckdb.sql(sql).fetchall()
    rows = lambda sql: [dict(zip(r.columns, v)) for r in [duckdb.sql(sql)] for v in r.fetchall()]

    tot = rows(f"""
        SELECT count(*) txs, count(DISTINCT account) accounts,
               sum(fee_drops) / 1e6 fees_xrp,
               sum(delivered_drops) FILTER (WHERE tx_type = 'Payment') / 1e6 xrp_delivered,
               count(*) FILTER (WHERE result = 'tesSUCCESS') ok
        FROM {tx}""")[0]
    led = rows(f"""
        SELECT count(*) ledgers, min(ledger_index) first_ledger, max(ledger_index) last_ledger,
               strftime(min(close_time), '%Y-%m-%dT%H:%M:%SZ') t0, strftime(max(close_time), '%Y-%m-%dT%H:%M:%SZ') t1,
               (max_by(total_drops, ledger_index) - min_by(total_drops, ledger_index)) / -1e6 burned_xrp,
               date_diff('second', min(close_time), max(close_time)) / nullif(count(*) - 1, 0) avg_close_s
        FROM {lg}""")[0]

    dataset = rows(f"""
        SELECT count(*) ledgers, sum(tx_count) txs, min(ledger_index) first_ledger, max(ledger_index) last_ledger,
               strftime(min(close_time), '%Y-%m-%dT%H:%M:%SZ') t0
        FROM {lg_all}""")[0]
    dataset.update(files=len(all_names), bytes=sum((args.data / d / n).stat().st_size
                                                   for n in all_names for d in ("ledgers", "transactions")))

    progress = {**parse_log(log), "log": log.name}
    if progress.get("start") is not None:  # count what is on disk in the range, across all runs
        spans = [tuple(int(x) for x in n.split(".")[0].split("-")) for n in all_names]
        progress["done"] = sum(max(0, min(hi, progress["end"]) - max(lo, progress["start"]) + 1) for lo, hi in spans)
    system = system_info()
    if progress["state"] in ("running", "stopped"):
        progress["state"] = "running" if system["exporter_running"] else "stopped"

    data = {
        "system": system,
        "dataset": dataset,
        "coverage": rows(f"""
            SELECT strftime(close_time, '%Y-%m-%d') AS day, count(*) ledgers
            FROM {lg_all} WHERE close_time >= now() - INTERVAL 366 DAY GROUP BY 1 ORDER BY 1"""),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "progress": progress,
        "refresh_minute": REFRESH_MINUTE,
        "goal": GOAL,
        "totals": {**tot, **led},
        "hourly": rows(f"""
            SELECT strftime(date_trunc('hour', close_time AT TIME ZONE 'UTC'), '%Y-%m-%dT%H:00Z') AS hour,
                   count(*) txs, count(*) FILTER (WHERE result = 'tesSUCCESS') ok, count(DISTINCT ledger_index) ledgers
            FROM {tx} GROUP BY 1 ORDER BY 1"""),
        "types": rows(f"""
            SELECT tx_type AS type, count(*) n, count(*) FILTER (WHERE result = 'tesSUCCESS') ok
            FROM {tx} GROUP BY 1 ORDER BY 2 DESC"""),
        "results": rows(f"SELECT result, count(*) n FROM {tx} GROUP BY 1 ORDER BY 2 DESC LIMIT 12"),
        "top_accounts": rows(f"""
            SELECT account, count(*) n, mode(tx_type) main_type,
                   count(*) FILTER (WHERE result = 'tesSUCCESS') ok
            FROM {tx} GROUP BY 1 ORDER BY 2 DESC LIMIT 10"""),
        "iou": [{**r, "currency": currency(r["currency"])} for r in rows(f"""
            SELECT delivered_currency AS currency, delivered_issuer AS issuer, count(*) n
            FROM {tx} WHERE tx_type = 'Payment' AND result = 'tesSUCCESS' AND delivered_currency IS NOT NULL
            GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 10""")],
        "files": [{"name": f.name, "ledgers_mb": round(f.stat().st_size / 1e6, 2),
                   "tx_mb": round((args.data / "transactions" / f.name).stat().st_size / 1e6, 2)}
                  for f in (args.data / "ledgers" / n for n in names)],
        "chunk_txs": dict(q(f"""
            SELECT regexp_extract(filename, '[^/]+$'), count(*) FROM read_parquet({tx_files}, filename = true) GROUP BY 1""")),
    }
    template = (HERE / "export_template.html").read_text()
    html = template.replace("/*__DATA__*/null", json.dumps(data, default=str))
    # Write atomically: a plain write_text() truncates the file in place, so a dev-server
    # file watcher (e.g. Live Server) can serve a half-written page mid-write — the browser
    # then shows raw JS as text because it never saw a complete <script> tag. Writing to a
    # temp file and renaming it means readers always see either the old or the new file whole.
    tmp = args.out.with_suffix(args.out.suffix + ".tmp")
    tmp.write_text(html)
    tmp.replace(args.out)
    print(f"wrote {args.out} ({data['totals']['txs']:,} txs, {data['totals']['ledgers']:,} ledgers)")


if __name__ == "__main__":
    main()
