"""Shared settings for the graph stage: paths, the DuckDB connection, versions, assets, writing.

Every graph job runs under the host rules in AGENTS.md: DuckDB at 2 threads / 4 GB, one BLAS thread,
data and temp files under /local/data/xrpl, no calls to the local node.
"""

import json
import logging
import os
import resource
import time
from datetime import date, timedelta
from pathlib import Path

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")  # before numpy/scipy load: single-threaded BLAS is deterministic

import duckdb  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

GRAPH_VERSION = "1.1"  # 1.1: funding rule keeps one creation per account
SEED = 20261010
DATA = Path(os.environ.get("XRPL_DATA", "/local/data/xrpl"))
RLUSD = "524C555344000000000000000000000000000000.rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
ALL = "*"                      # all assets together: counts only
EXTRA_ASSETS = 8               # assets analysed besides XRP and RLUSD, by transfer count
SIG_DIGITS = 12                # floats written by graph code are rounded to this many significant digits
RSS_LIMIT_MB = 6144

log = logging.getLogger("graph")


class Paths:
    def __init__(self, data: Path = DATA):
        self.data = data
        self.silver = data / "silver"
        self.gold = data / "gold"
        self.home = data / "pipeline"
        self.tmp = self.home / "tmp"
        self.logs = self.home / "logs"
        self.state = self.home / "graph_state.duckdb"
        self.lock = Path("/tmp/xrpl-graph.lock") if data == DATA else self.home / "graph.lock"

    def part(self, table: str, month: str) -> Path:
        return self.gold / table / f"month={month}.parquet"


def connect(p: Paths) -> duckdb.DuckDBPyConnection:
    p.tmp.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"SET threads = 2; SET memory_limit = '4GB'; SET TimeZone = 'UTC'; "
                f"SET temp_directory = '{p.tmp}'; SET preserve_insertion_order = false; "
                f"SET enable_progress_bar = false")
    return con


def month_bounds(month: str) -> tuple[date, date]:
    """First day of the month and first day of the next."""
    d0 = date.fromisoformat(month + "-01")
    return d0, (d0 + timedelta(days=32)).replace(day=1)


def prev_month(month: str) -> str:
    d0, _ = month_bounds(month)
    return f"{d0 - timedelta(days=1):%Y-%m}"


def q(path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def silver_month_files(p: Paths, table: str, month: str) -> list[str]:
    """Silver files that can hold rows of `month`: its own folder plus the neighbours (chunks straddle months)."""
    files = []
    for m in (prev_month(month), month, f"{month_bounds(month)[1]:%Y-%m}"):
        files += sorted(str(f) for f in (p.silver / table / f"month={m}").glob("*.parquet"))
    return files


def edge_months(p: Paths) -> list[str]:
    return sorted(f.stem.split("=", 1)[1] for f in (p.gold / "transfer_edges_monthly").glob("month=*.parquet"))


def assets_for_month(con, p: Paths, month: str) -> list[str]:
    """XRP, RLUSD and the next EXTRA_ASSETS issued assets by transfer count, then '*'."""
    top = [r[0] for r in con.execute(f"""
        SELECT asset FROM read_parquet({q(p.part('transfer_edges_monthly', month))})
        WHERE asset NOT IN ('XRP', 'MPT', '{RLUSD}')
        GROUP BY asset ORDER BY sum(n_transfers) DESC, asset LIMIT {EXTRA_ASSETS}""").fetchall()]
    return ["XRP", RLUSD, *top, ALL]


def asset_label(asset: str) -> str:
    """Short readable name: XRP, RLUSD-rMxCKb, all."""
    if asset == ALL:
        return "all"
    if asset in ("XRP", "MPT"):
        return asset
    cur, iss = asset.split(".", 1)
    if len(cur) == 40:
        raw = bytes.fromhex(cur).rstrip(b"\0")
        code = raw.decode("ascii", "replace") if raw and raw[0] != 0 and all(32 <= b < 127 for b in raw) else cur[:8]
    else:
        code = cur
    return f"{code}-{iss[:6]}"


def round_sig(x: float | None) -> float | None:
    if x is None or x != x or x == 0:
        return x
    return float(f"{x:.{SIG_DIGITS}g}")


def write_table(table: pa.Table, out: Path, sort_by: list[str]) -> int:
    """Sort by key, then write Parquet atomically (.tmp + rename). Returns the row count."""
    out.parent.mkdir(parents=True, exist_ok=True)
    if table.num_rows:
        table = table.sort_by([(c, "ascending") for c in sort_by])
    tmp = out.with_name(out.name + ".tmp")
    pq.write_table(table, tmp, compression="zstd")
    os.replace(tmp, out)
    return table.num_rows


def copy_atomic(con, sql: str, out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    rows = con.execute(f"COPY ({sql}) TO {q(tmp)} (FORMAT parquet, COMPRESSION zstd)").fetchone()[0]
    os.replace(tmp, out)
    return rows


def peak_rss_mb() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024


def check_rss(step: str) -> None:
    mb = peak_rss_mb()
    if mb > RSS_LIMIT_MB:
        raise MemoryError(f"{step}: peak RSS {mb} MB is over the {RSS_LIMIT_MB} MB budget")


class Timer:
    def __init__(self, what: str):
        self.what = what

    def __enter__(self):
        self.t = time.time()
        return self

    def __exit__(self, *exc):
        self.seconds = time.time() - self.t
        if exc[0] is None:
            log.info("%s: %.1f s, peak RSS %d MB", self.what, self.seconds, peak_rss_mb())


def dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))
