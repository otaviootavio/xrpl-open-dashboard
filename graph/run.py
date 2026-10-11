"""Graph stage of the XRPL transfer graph: entities, metrics and views from gold transfer_edges_monthly.

    uv run graph/run.py status
    uv run graph/run.py firstlook --month 2026-09        # E2: hubs, tails, backbone prototype for one month
    uv run graph/run.py all --months 2026-08,2026-09     # entities -> metrics -> views, skipping unchanged months
    uv run graph/run.py entities|metrics|views --months M[,M...] [--force]
    uv run graph/run.py ego rADDRESS --month 2026-09 [--asset XRP]

State lives in pipeline/graph_state.duckdb (not state.duckdb: DuckDB allows one writer per file and
pipeline/run.py holds that one while it runs). A month is redone only when its inputs' fingerprint or
GRAPH_VERSION changed. Run long jobs as `nice -n 10 ionice -c3 uv run graph/run.py ...`.
"""

import argparse
import fcntl
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

import common
from common import ALL, DATA, GRAPH_VERSION, RLUSD, Paths, asset_label, connect, edge_months, peak_rss_mb, prev_month, q

log = logging.getLogger("graph")
REPORT = Path(__file__).resolve().parent.parent / "report"

STATE_DDL = """CREATE TABLE IF NOT EXISTS graph_parts (
    step VARCHAR, month VARCHAR, fingerprint UBIGINT, graph_version VARCHAR, seconds DOUBLE,
    peak_rss_mb BIGINT, detail VARCHAR, done_at TIMESTAMPTZ, PRIMARY KEY (step, month))"""

# each step's inputs: the parts whose content decides its output (a change anywhere redoes the step)
INPUTS = {
    "entities": lambda m: [("transfer_edges_monthly", m), ("dest_tag_monthly", m)],
    "metrics": lambda m: [("graph_entity_edges", m), ("graph_entities", m), ("graph_entity_edges", prev_month(m))],
    "views": lambda m: [("graph_nodes", m), ("graph_communities", m), ("graph_cycles", m)],
}


def fingerprint(con, p: Paths, step: str, month: str) -> int:
    parts = []
    for table, m in INPUTS[step](month):
        f = p.part(table, m)
        if f.exists():
            parts.append(f"(SELECT hash('{table}/{m}', count(*), sum(hash(x))) FROM read_parquet({q(f)}) x)")
        else:
            parts.append(f"hash('{table}/{m} missing')")
    return con.execute(f"SELECT hash({', '.join(parts)})").fetchone()[0]


def state(p: Paths) -> duckdb.DuckDBPyConnection:
    st = duckdb.connect(str(p.state))
    st.execute(STATE_DDL)
    return st


def run_step(con, st, p: Paths, step: str, month: str, force: bool) -> None:
    fp = fingerprint(con, p, step, month)
    done = st.execute("SELECT fingerprint, graph_version FROM graph_parts WHERE step = ? AND month = ?",
                      [step, month]).fetchone()
    if done and done == (fp, GRAPH_VERSION) and not force:
        log.info("%s %s: unchanged, skipped", month, step)
        return
    t = time.time()
    if step == "entities":
        import entities
        detail = entities.build(con, p, month)
    elif step == "metrics":
        import metrics
        detail = metrics.build(con, p, month)
    else:
        import views
        out = REPORT / "graph"
        detail = {"views": len(entries := views.build(con, p, out, month))}
        views.write_index(out, entries)
        views.build_loops(con, p, out)
    common.check_rss(f"{month} {step}")
    st.execute("INSERT OR REPLACE INTO graph_parts VALUES (?, ?, ?, ?, ?, ?, ?, now())",
               [step, month, fp, GRAPH_VERSION, round(time.time() - t, 1), peak_rss_mb(), common.dumps(detail)])
    log.info("%s %s: done in %.1f s, peak RSS %d MB: %s", month, step, time.time() - t, peak_rss_mb(), detail)


def cmd_status(con, st, p: Paths) -> None:
    months = edge_months(p)
    print(f"transfer_edges_monthly: {len(months)} months ({months[0] if months else '-'} .. {months[-1] if months else '-'})")
    for step in INPUTS:
        rows = st.execute("SELECT month, graph_version, seconds, peak_rss_mb, done_at FROM graph_parts "
                          "WHERE step = ? ORDER BY month", [step]).fetchall()
        stale = [m for m, *_ in rows if m in months and st.execute(
            "SELECT fingerprint FROM graph_parts WHERE step = ? AND month = ?", [step, m]).fetchone()[0]
            != fingerprint(con, p, step, m)]
        last = rows[-1] if rows else None
        print(f"{step:<9} {len(rows)} months done; newest {last[0] if last else '-'} "
              f"(v{last[1] if last else '-'}, {last[2] if last else '-'} s, {last[3] if last else '-'} MB, "
              f"{last[4]:%Y-%m-%d %H:%M} UTC)" if last else f"{step:<9} nothing done yet")
        if stale:
            print(f"          inputs changed since: {', '.join(stale)}")
        missing = [m for m in months if m not in {r[0] for r in rows}]
        if missing:
            print(f"          not done: {', '.join(missing)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["status", "firstlook", "entities", "metrics", "views", "all", "ego"])
    ap.add_argument("address", nargs="?", help="ego: an account address")
    ap.add_argument("--data", type=Path, default=DATA)
    ap.add_argument("--months", help="comma-separated YYYY-MM (default: every month with edges)")
    ap.add_argument("--month", help="firstlook / ego: one YYYY-MM")
    ap.add_argument("--asset", default="XRP", help="ego: XRP, RLUSD, all or an asset key")
    ap.add_argument("--force", action="store_true", help="redo even when inputs are unchanged")
    args = ap.parse_args()

    p = Paths(args.data)
    p.logs.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%SZ")
    fmt.converter = time.gmtime
    for h in (logging.StreamHandler(), logging.FileHandler(p.logs / "graph.log")):
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)

    con = connect(p)
    if args.cmd == "status":
        cmd_status(con, duckdb.connect(str(p.state), read_only=True) if p.state.exists() else state(p), p)
        return

    lock = open(p.lock, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("another graph run holds %s; exiting", p.lock)
        return
    log.info("graph run %s (graph_version %s) at %s", " ".join(sys.argv[1:]), GRAPH_VERSION,
             datetime.now(timezone.utc).isoformat(timespec="seconds"))

    if args.cmd == "firstlook":
        import firstlook
        firstlook.run(con, p, args.month or edge_months(p)[-2])
        return
    if args.cmd == "ego":
        import views
        asset = {"RLUSD": RLUSD, "all": ALL}.get(args.asset, args.asset)
        e = views.ego_on_demand(con, p, REPORT / "graph", args.month or edge_months(p)[-1], asset, args.address)
        print(f"wrote report/graph/{e['file']} ({e['n_nodes']} nodes); open report/graph.html#ego~{e['month']}~"
              f"{asset_label(asset)}~{e['centre']}")
        return

    months = args.months.split(",") if args.months else edge_months(p)
    steps = ["entities", "metrics", "views"] if args.cmd == "all" else [args.cmd]
    st = state(p)
    for month in sorted(months):
        if not p.part("transfer_edges_monthly", month).exists():
            log.warning("%s: no transfer_edges_monthly part; skipped", month)
            continue
        for step in steps:
            run_step(con, st, p, step, month, args.force)


if __name__ == "__main__":
    main()
