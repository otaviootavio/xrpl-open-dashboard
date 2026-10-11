"""Incremental bronze -> silver -> gold pipeline for the XRPL Open Data Project.

    uv run pipeline/run.py update  [--tables T,..] [--gold G,..] [--months YYYY-MM,..] [--limit N]
    uv run pipeline/run.py rebuild [--from-month YYYY-MM] [--tables T,..]
    uv run pipeline/run.py invalidate --tables T,.. [--from-month YYYY-MM]   # drop silver; redo with update --limit
    uv run pipeline/run.py regold  [--from-month YYYY-MM] [--months M,..] [--gold G,..]   # gold only, from existing silver
    uv run pipeline/run.py check   [--days YYYY-MM-DD..YYYY-MM-DD]
    uv run pipeline/run.py compact
    uv run pipeline/run.py status

Bronze (transactions/, ledgers/) is only ever read. Silver is one Parquet file per bronze chunk
per table; gold is small per-day / per-month / running tables. Every output is written to .tmp and
renamed, and recorded in pipeline/state.duckdb only after the rename, so a killed run is redone
cleanly by the next one. Run under `nice -n 10 ionice -c3`: this host is a live validator.
See the "Technical spec" tab of the PRD and .claude/skills/xrpl-medallion/SKILL.md.
"""

import argparse
import fcntl
import json
import logging
import os
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import codec  # noqa: E402
from tiers import GOLD, GOLD_BY_NAME, SILVER, SILVER_BY_NAME  # noqa: E402

SQL = HERE / "sql"
THREADS, MEMORY = 2, "4GB"  # leave the CPU and RAM to the validator
XRPLD_RPC = "http://127.0.0.1:5005"

log = logging.getLogger("pipeline")


# ---------------------------------------------------------------- paths, connection, state

class Paths:
    def __init__(self, data: Path):
        self.data = data
        self.tx = data / "transactions"
        self.lg = data / "ledgers"
        self.silver = data / "silver"
        self.gold = data / "gold"
        self.home = data / "pipeline"
        self.state = self.home / "state.duckdb"
        self.tmp = self.home / "tmp"
        self.logs = self.home / "logs"
        self.lock = self.home / "run.lock"


STATE_DDL = """
CREATE TABLE IF NOT EXISTS bronze_chunks (
    chunk VARCHAR PRIMARY KEY, first_ledger BIGINT, last_ledger BIGINT,
    day_min DATE, day_max DATE, month VARCHAR, tx_rows BIGINT,
    bronze_bytes BIGINT, bronze_mtime DOUBLE, seen_at TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS silver_chunks (
    table_name VARCHAR, chunk VARCHAR, bronze_bytes BIGINT, bronze_mtime DOUBLE,
    out_path VARCHAR, rows BIGINT, day_min DATE, day_max DATE, done_at TIMESTAMPTZ,
    PRIMARY KEY (table_name, chunk));
CREATE TABLE IF NOT EXISTS gold_parts (
    table_name VARCHAR, part VARCHAR, rows BIGINT, done_at TIMESTAMPTZ,
    PRIMARY KEY (table_name, part));
CREATE TABLE IF NOT EXISTS runs (
    run_id BIGINT, cmd VARCHAR, started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ,
    status VARCHAR, chunks INT, parts INT, error VARCHAR);
"""


def connect(p: Paths) -> duckdb.DuckDBPyConnection:
    for d in (p.home, p.tmp, p.logs, p.silver, p.gold):
        d.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(p.state))
    con.execute(f"SET threads = {THREADS}; SET memory_limit = '{MEMORY}'; SET TimeZone = 'UTC'; "
                f"SET temp_directory = '{p.tmp}'; SET preserve_insertion_order = false")
    con.execute(STATE_DDL)
    con.execute((SQL / "macros.sql").read_text())
    con.create_function("nft_issuer", codec.nft_issuer, ["VARCHAR"], "VARCHAR")
    return con


def q(path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def file_list(paths) -> str:
    return "[" + ", ".join(q(x) for x in paths) + "]"


def copy_atomic(con, sql: str, out: Path) -> int:
    """Write a query to Parquet via .tmp + rename; returns the row count."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    rows = con.execute(f"COPY ({sql}) TO {q(tmp)} (FORMAT parquet, COMPRESSION zstd)").fetchone()[0]
    os.replace(tmp, out)
    return rows


def clean_tmp(p: Paths) -> None:
    for root in (p.silver, p.gold):
        for f in root.rglob("*.tmp"):
            f.unlink()


# ---------------------------------------------------------------- bronze discovery

def discover(con, p: Paths, months: set[str] | None) -> list[tuple]:
    """Register new or changed bronze chunks; return [(chunk, bytes, mtime, month)] oldest first."""
    names = sorted(f.name for f in p.tx.glob("*.parquet") if (p.lg / f.name).exists())
    known = {r[0]: (r[1], r[2], r[3]) for r in con.execute(
        "SELECT chunk, bronze_bytes, bronze_mtime, month FROM bronze_chunks").fetchall()}
    out = []
    new = 0
    for name in names:
        st = (p.tx / name).stat()
        k = known.get(name)
        if k is None or k[0] != st.st_size or k[1] != st.st_mtime:
            first, last, dmin, dmax, month = con.execute(f"""
                SELECT min(ledger_index), max(ledger_index), min(close_time)::DATE, max(close_time)::DATE,
                       strftime(arg_min(close_time, ledger_index), '%Y-%m')
                FROM read_parquet({q(p.lg / name)})""").fetchone()
            rows = con.execute(f"SELECT count(*) FROM read_parquet({q(p.tx / name)})").fetchone()[0]
            con.execute("INSERT OR REPLACE INTO bronze_chunks VALUES (?,?,?,?,?,?,?,?,?,now())",
                        [name, first, last, dmin, dmax, month, rows, st.st_size, st.st_mtime])
            k = (st.st_size, st.st_mtime, month)
            new += 1
            if new % 500 == 0:
                log.info("bronze: registered %d new chunks so far", new)
        if months is None or k[2] in months:
            out.append((name, k[0], k[1], k[2]))
    return out


# ---------------------------------------------------------------- silver

def todo_tables(con, chunk, size, mtime, tables) -> list[str]:
    done = {r[0] for r in con.execute(
        "SELECT table_name FROM silver_chunks WHERE chunk = ? AND bronze_bytes = ? AND bronze_mtime = ?",
        [chunk, size, mtime]).fetchall()}
    return [t for t in tables if t not in done]


def process_chunk(con, p: Paths, chunk: str, size: int, mtime: float, month: str, tables: list[str]) -> None:
    con.execute(f"CREATE OR REPLACE TEMP VIEW src AS SELECT * FROM read_parquet({q(p.tx / chunk)})")
    specs = [SILVER_BY_NAME[t] for t in tables]
    if any(s.nodes or s.pool_deltas for s in specs):
        users = [s for s in specs if s.nodes or s.pool_deltas]
        nodes_sql = (SQL / "silver/_nodes.sql").read_text()
        if all(s.tx_filter for s in users):
            where = " OR ".join(f"({s.tx_filter})" for s in users)
            nodes_sql = nodes_sql.replace("FROM src)", f"FROM src WHERE {where})")
        con.execute(f"CREATE OR REPLACE TEMP TABLE nodes AS {nodes_sql}")
    if any(s.pool_deltas for s in specs):
        con.execute(f"CREATE OR REPLACE TEMP TABLE pool_deltas AS {(SQL / 'silver/_pool_deltas.sql').read_text()}")
    dmin, dmax = con.execute("SELECT day_min, day_max FROM bronze_chunks WHERE chunk = ?", [chunk]).fetchone()
    for t in tables:
        out = p.silver / t / f"month={month}" / chunk
        rows = copy_atomic(con, (SQL / f"silver/{t}.sql").read_text(), out)
        con.execute("INSERT OR REPLACE INTO silver_chunks VALUES (?,?,?,?,?,?,?,?,now())",
                    [t, chunk, size, mtime, str(out), rows, dmin, dmax])
    con.execute("DROP TABLE IF EXISTS nodes; DROP TABLE IF EXISTS pool_deltas")


# ---------------------------------------------------------------- gold

def silver_files(con, table: str, d0: date, d1: date) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT DISTINCT out_path FROM silver_chunks WHERE table_name = ? AND day_min <= ? AND day_max >= ? "
        "ORDER BY 1", [table, d1, d0]).fetchall()]


def complete(con, tables, d0: date, d1: date) -> bool:
    """True when every bronze chunk overlapping [d0, d1] has been processed for every table."""
    need = con.execute("SELECT count(*) FROM bronze_chunks WHERE day_min <= ? AND day_max >= ?", [d1, d0]).fetchone()[0]
    for t in tables:
        have = con.execute("SELECT count(*) FROM silver_chunks WHERE table_name = ? AND day_min <= ? AND day_max >= ?",
                           [t, d1, d0]).fetchone()[0]
        if have < need:
            return False
    return need > 0


def ledger_view(con, p: Paths, d0: date, d1: date, chunks: list[str] | None = None) -> None:
    if chunks is None:
        chunks = [r[0] for r in con.execute(
            "SELECT chunk FROM bronze_chunks WHERE day_min <= ? AND day_max >= ? ORDER BY 1",
            [d1 + timedelta(days=1), d0 - timedelta(days=1)]).fetchall()]
    con.execute(f"""CREATE OR REPLACE TEMP VIEW lg_src AS
        SELECT * FROM read_parquet({file_list(p.lg / c for c in chunks)})
        WHERE close_time >= DATE '{d0 - timedelta(days=1)}' AND close_time < DATE '{d1 + timedelta(days=2)}'""")


def silver_views(con, reads, d0: date, d1: date, files_by_table=None) -> None:
    for t in reads:
        files = files_by_table[t] if files_by_table else silver_files(con, t, d0, d1)
        con.execute(f"""CREATE OR REPLACE TEMP VIEW s_{t} AS
            SELECT * FROM read_parquet({file_list(files)}, union_by_name = true)
            WHERE close_time >= DATE '{d0}' AND close_time < DATE '{d1 + timedelta(days=1)}'""")


def fill(sql: str, d0: date, d1: date) -> str:
    return sql.replace("{day_from}", f"DATE '{d0}'").replace("{day_to}", f"DATE '{d1}'")


def month_bounds(d: date) -> tuple[date, date]:
    first = d.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return first, nxt - timedelta(days=1)


def run_daily(con, p: Paths, g, days: set[date]) -> int:
    parts = 0
    by_month: dict[date, list[date]] = {}
    for d in sorted(days):
        by_month.setdefault(d.replace(day=1), []).append(d)
    for _, ds in sorted(by_month.items()):
        ds = [d for d in ds if complete(con, g.reads, d, d)]
        if not ds:
            continue
        d0, d1 = ds[0], ds[-1]
        silver_views(con, g.reads, d0, d1)
        if g.ledgers:
            ledger_view(con, p, d0, d1)
        con.execute(f"CREATE OR REPLACE TEMP TABLE g_out AS {fill((SQL / f'gold/{g.name}.sql').read_text(), d0, d1)}")
        for d in ds:
            rows = copy_atomic(con, f"SELECT * FROM g_out WHERE day = DATE '{d}'", p.gold / g.name / f"day={d}.parquet")
            con.execute("INSERT OR REPLACE INTO gold_parts VALUES (?,?,?,now())", [g.name, f"day={d}", rows])
            parts += 1
    return parts


def run_monthly(con, p: Paths, g, days: set[date]) -> int:
    parts = 0
    for m in sorted({d.replace(day=1) for d in days}):
        d0, d1 = month_bounds(m)
        if not complete(con, g.reads, d0, d1):  # a month still filling is fine; a half-processed one is not
            continue
        silver_views(con, g.reads, d0, d1)
        rows = copy_atomic(con, fill((SQL / f"gold/{g.name}.sql").read_text(), d0, d1),
                           p.gold / g.name / f"month={m:%Y-%m}.parquet")
        con.execute("INSERT OR REPLACE INTO gold_parts VALUES (?,?,?,now())", [g.name, f"month={m:%Y-%m}", rows])
        parts += 1
    return parts


def run_running(con, p: Paths, g, new_chunks: list[str]) -> int:
    if not new_chunks:
        return 0
    files = {t: [r[0] for r in con.execute(
        f"SELECT out_path FROM silver_chunks WHERE table_name = ? AND chunk IN ({','.join('?' * len(new_chunks))})",
        [t, *new_chunks]).fetchall()] for t in g.reads}
    if any(not f for f in files.values()):
        return 0
    lo, hi = con.execute(f"SELECT min(day_min), max(day_max) FROM bronze_chunks WHERE chunk IN ({','.join('?' * len(new_chunks))})",
                         new_chunks).fetchone()
    silver_views(con, g.reads, lo, hi, files)
    if g.ledgers:
        ledger_view(con, p, lo, hi)  # with a day of neighbours, so boundary close gaps are seen
    con.execute(f"CREATE OR REPLACE TEMP TABLE cand AS {(SQL / f'gold/{g.name}.sql').read_text()}")
    out = p.gold / f"{g.name}.parquet"
    if out.exists():
        con.execute(f"CREATE OR REPLACE TEMP VIEW g_existing AS SELECT * FROM read_parquet({q(out)})")
    else:
        con.execute("CREATE OR REPLACE TEMP VIEW g_existing AS SELECT * FROM cand WHERE false")
    con.execute(f"CREATE OR REPLACE TEMP TABLE g_out AS {(SQL / f'gold/{g.name}.merge.sql').read_text()}")
    rows = copy_atomic(con, "SELECT * FROM g_out", out)
    con.execute("INSERT OR REPLACE INTO gold_parts VALUES (?,?,?,now())", [g.name, "all", rows])
    return 1


# ---------------------------------------------------------------- commands

def cmd_update(con, p: Paths, args) -> tuple[int, int]:
    silver = args.tables.split(",") if args.tables else [t.name for t in SILVER]
    unknown = [t for t in silver if t not in SILVER_BY_NAME]
    if unknown:
        sys.exit(f"unknown silver tables: {unknown}")
    months = set(args.months.split(",")) if args.months else None
    chunks = discover(con, p, months)
    work = [(c, s, m, mo, todo_tables(con, c, s, m, silver)) for c, s, m, mo in chunks]
    work = [w for w in work if w[4]]
    if args.limit:
        work = work[:args.limit]
    log.info("silver: %d chunks to process (%s)", len(work), ", ".join(silver))
    days: set[date] = set()
    new_chunks = []
    t0 = time.time()
    for i, (chunk, size, mtime, month, tables) in enumerate(work, 1):
        process_chunk(con, p, chunk, size, mtime, month, tables)
        d0, d1 = con.execute("SELECT day_min, day_max FROM bronze_chunks WHERE chunk = ?", [chunk]).fetchone()
        days.update(d0 + timedelta(days=k) for k in range((d1 - d0).days + 1))
        new_chunks.append(chunk)
        if i % 25 == 0 or i == len(work):
            rate = (time.time() - t0) / i
            log.info("silver: %d/%d chunks, %.2f s/chunk, ~%.0f min left", i, len(work), rate, rate * (len(work) - i) / 60)

    gold = [GOLD_BY_NAME[g] for g in args.gold.split(",")] if args.gold else \
        [g for g in GOLD if set(g.reads) <= set(silver) | done_tables(con)]
    parts = 0
    for g in gold:
        t = time.time()
        if g.kind == "daily":
            n = run_daily(con, p, g, days)
        elif g.kind == "monthly":
            n = run_monthly(con, p, g, days)
        else:
            n = run_running(con, p, g, new_chunks)
        parts += n
        if n:
            log.info("gold: %s, %d parts in %.1f s", g.name, n, time.time() - t)
    return len(new_chunks), parts


def done_tables(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT DISTINCT table_name FROM silver_chunks").fetchall()}


def invalidate(con, tables: list[str], since: str) -> None:
    """Delete silver files and state rows of `tables` from month `since` on; the next update redoes them."""
    for t in tables:
        for row in con.execute("SELECT s.out_path FROM silver_chunks s JOIN bronze_chunks b USING (chunk) "
                               "WHERE s.table_name = ? AND b.month >= ?", [t, since]).fetchall():
            Path(row[0]).unlink(missing_ok=True)
        con.execute("DELETE FROM silver_chunks WHERE table_name = ? AND chunk IN "
                    "(SELECT chunk FROM bronze_chunks WHERE month >= ?)", [t, since])


def cmd_invalidate(con, p: Paths, args) -> tuple[int, int]:
    """After a silver parse-rule change on a big range: drop the old silver, then reprocess with
    `update --tables T --limit N` in batches (`rebuild` would do it all in one long run)."""
    if not args.tables:
        sys.exit("invalidate needs --tables")
    tables = args.tables.split(",")
    since = args.from_month or "0000-00"
    log.info("invalidate: deleting silver %s from %s", tables, since)
    invalidate(con, tables, since)
    return 0, 0


def cmd_rebuild(con, p: Paths, args) -> tuple[int, int]:
    tables = args.tables.split(",") if args.tables else [t.name for t in SILVER]
    since = args.from_month or "0000-00"
    log.info("rebuild: deleting silver %s and dependent gold from %s", tables, since)
    invalidate(con, tables, since)
    for g in GOLD:
        if not set(g.reads) & set(tables) and g.reads:
            continue
        if g.kind == "running":
            (p.gold / f"{g.name}.parquet").unlink(missing_ok=True)
            con.execute("DELETE FROM gold_parts WHERE table_name = ?", [g.name])
            continue
        for f in (p.gold / g.name).glob("*.parquet"):
            key = f.stem.split("=", 1)[1][:7]
            if key >= since:
                f.unlink()
                con.execute("DELETE FROM gold_parts WHERE table_name = ? AND part = ?", [g.name, f.stem])
    args.months, args.limit, args.gold = None, None, None
    args.tables = ",".join(tables)
    res = cmd_update(con, p, args)
    if since != "0000-00":
        # running tables were deleted whole: rebuild them from every chunk, not only the redone months
        all_chunks = [r[0] for r in con.execute("SELECT chunk FROM bronze_chunks ORDER BY 1").fetchall()]
        for g in GOLD:
            if g.kind == "running":
                run_running(con, p, g, all_chunks)
    return res


def cmd_regold(con, p: Paths, args) -> tuple[int, int]:
    """Recompute gold from existing silver (after a gold SQL change); silver is not touched."""
    since = args.from_month or "0000-00"
    gold = [GOLD_BY_NAME[g] for g in args.gold.split(",")] if args.gold else GOLD
    days: set[date] = set()
    months = set(args.months.split(",")) if args.months else None
    for d0, d1, mo in con.execute("SELECT day_min, day_max, month FROM bronze_chunks WHERE month >= ?", [since]).fetchall():
        if months and mo not in months:
            continue
        days.update(d0 + timedelta(days=k) for k in range((d1 - d0).days + 1))
    all_chunks = [r[0] for r in con.execute("SELECT chunk FROM bronze_chunks ORDER BY 1").fetchall()]
    parts = 0
    for g in gold:
        t = time.time()
        if g.kind == "running":
            (p.gold / f"{g.name}.parquet").unlink(missing_ok=True)
            n = run_running(con, p, g, all_chunks)
        else:
            n = (run_daily if g.kind == "daily" else run_monthly)(con, p, g, days)
        parts += n
        log.info("regold: %s, %d parts in %.1f s", g.name, n, time.time() - t)
    return 0, parts


def cmd_check(con, p: Paths, args) -> bool:
    import checks
    return checks.run(con, p, args)


def cmd_compact(con, p: Paths, args) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=2)).date().replace(day=1)
    for t in SILVER:
        for mdir in sorted((p.silver / t.name).glob("month=*")):
            month = mdir.name.split("=", 1)[1]
            if month >= f"{cutoff:%Y-%m}":
                continue
            files = sorted(f for f in mdir.glob("*.parquet") if f.name != "all.parquet")
            if not files:
                continue
            existing = mdir / "all.parquet"
            sources = files + ([existing] if existing.exists() else [])
            rows = copy_atomic(con, f"SELECT * FROM read_parquet({file_list(sources)}, union_by_name = true) "
                                    "ORDER BY ledger_index", mdir / "all.parquet.new")
            os.replace(mdir / "all.parquet.new", existing)
            con.execute(f"UPDATE silver_chunks SET out_path = ? WHERE table_name = ? AND out_path IN ({','.join('?' * len(sources))})",
                        [str(existing), t.name, *map(str, sources)])
            for f in files:
                f.unlink()
            log.info("compact: %s %s, %d files -> all.parquet (%d rows)", t.name, month, len(files), rows)


def validator_tip() -> int | None:
    try:
        req = urllib.request.Request(XRPLD_RPC, data=json.dumps({"method": "server_info"}).encode(),
                                     headers={"Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req, timeout=5))["result"]["info"]["validated_ledger"]["seq"]
    except Exception:
        return None


def cmd_status(con, p: Paths, args) -> None:
    discover(con, p, set())  # register new chunks only
    bronze = con.execute("SELECT max(last_ledger), count(*) FROM bronze_chunks").fetchone()
    tip = validator_tip()
    print(f"bronze  last ledger {bronze[0]:,} in {bronze[1]:,} chunks"
          + (f"; validator at {tip:,} (lag {tip - bronze[0]:,} ledgers)" if tip and bronze[0] else ""))
    for t in SILVER:
        r = con.execute("SELECT count(*), max(b.last_ledger), sum(s.rows) FROM silver_chunks s JOIN bronze_chunks b USING (chunk) "
                        "WHERE table_name = ?", [t.name]).fetchone()
        print(f"silver  {t.name:<20} {r[0]:>6,} chunks  last ledger {r[1] or 0:>13,}  rows {r[2] or 0:>14,}")
    for g in GOLD:
        r = con.execute("SELECT count(*), max(part), max(done_at) FROM gold_parts WHERE table_name = ?", [g.name]).fetchone()
        print(f"gold    {g.name:<20} {r[0]:>6,} parts  latest {r[1] or '-'}")
    last = con.execute("SELECT cmd, status, finished_at, error FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
    if last:
        print(f"last run: {last[0]} {last[1]} at {last[2]}" + (f" ({last[3]})" if last[3] else ""))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["update", "rebuild", "invalidate", "regold", "check", "compact", "status"])
    ap.add_argument("--data", type=Path, default=Path("/local/data/xrpl"))
    ap.add_argument("--tables", help="silver tables (default: all)")
    ap.add_argument("--gold", help="gold tables (default: those whose inputs are available)")
    ap.add_argument("--months", help="only bronze chunks of these months, e.g. 2026-09")
    ap.add_argument("--limit", type=int, help="process at most N chunks")
    ap.add_argument("--from-month", help="rebuild: from this month on (default: everything)")
    ap.add_argument("--days", help="check: YYYY-MM-DD..YYYY-MM-DD (default: everything in gold)")
    args = ap.parse_args()

    p = Paths(args.data)
    p.home.mkdir(parents=True, exist_ok=True)
    p.logs.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%SZ")
    fmt.converter = time.gmtime
    for h in (logging.StreamHandler(), logging.FileHandler(p.logs / f"run_{datetime.now(timezone.utc):%Y%m%d}.log")):
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)

    lock = open(p.lock, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("another pipeline run holds %s; exiting", p.lock)
        return

    con = connect(p)
    if args.cmd == "status":
        cmd_status(con, p, args)
        return
    clean_tmp(p)
    run_id = int(time.time() * 1000)
    con.execute("INSERT INTO runs VALUES (?, ?, now(), NULL, 'running', 0, 0, NULL)", [run_id, " ".join(sys.argv[1:])])
    status, chunks, parts, err = "ok", 0, 0, None
    try:
        if args.cmd == "update":
            chunks, parts = cmd_update(con, p, args)
        elif args.cmd == "rebuild":
            chunks, parts = cmd_rebuild(con, p, args)
        elif args.cmd == "invalidate":
            chunks, parts = cmd_invalidate(con, p, args)
        elif args.cmd == "regold":
            chunks, parts = cmd_regold(con, p, args)
        elif args.cmd == "check":
            status = "ok" if cmd_check(con, p, args) else "failed"
        elif args.cmd == "compact":
            cmd_compact(con, p, args)
    except BaseException as e:
        status, err = "error", f"{type(e).__name__}: {e}"
        log.exception("run failed")
        raise
    finally:
        con.execute("UPDATE runs SET finished_at = now(), status = ?, chunks = ?, parts = ?, error = ? WHERE run_id = ?",
                    [status, chunks, parts, err, run_id])
        log.info("%s finished: %s, %d chunks, %d gold parts", args.cmd, status, chunks, parts)
    if status == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
