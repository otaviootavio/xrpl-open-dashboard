"""Integrity checks for the Parquet export in /local/data/xrpl.

    uv run .claude/skills/xrpl-ledger-export/scripts/verify_export.py [--data /local/data/xrpl]

Checks: every transactions chunk has a ledgers chunk; no missing ledgers in the covered range;
no duplicate ledgers or transaction hashes; per-ledger tx_count matches the exported rows; and the
XRP burned (drop in total supply) matches the fees paid. Exits 1 if any check fails.
"""

import argparse
import threading
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import duckdb


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=Path("/local/data/xrpl"))
    root = ap.parse_args().data
    # Same limits as pipeline/run.py: leave the CPU and RAM to the validator; spill to the HDD array.
    (root / "pipeline" / "tmp").mkdir(parents=True, exist_ok=True)
    duckdb.sql(f"SET threads = 2; SET memory_limit = '4GB'; SET temp_directory = '{root}/pipeline/tmp'; "
               "SET preserve_insertion_order = false")

    tx_names = {f.name for f in (root / "transactions").glob("*.parquet")}
    lg_names = {f.name for f in (root / "ledgers").glob("*.parquet")}
    names = sorted(tx_names & lg_names)
    if not names:
        sys.exit(f"No chunks found under {root}")
    tx = "read_parquet([" + ",".join(f"'{root}/transactions/{n}'" for n in names) + "])"
    lg = "read_parquet([" + ",".join(f"'{root}/ledgers/{n}'" for n in names) + "])"
    one = lambda sql: duckdb.sql(sql).fetchone()

    lo, hi, n_led, n_distinct = one(f"SELECT min(ledger_index), max(ledger_index), count(*), count(DISTINCT ledger_index) FROM {lg}")
    burned = one(f"SELECT min_by(total_drops, ledger_index) - max_by(total_drops, ledger_index) FROM {lg}")[0]

    # Transaction checks, one chunk at a time. A global count(DISTINCT hash) over ~870M rows cannot
    # spill to disk and runs out of memory under the 4 GB cap. Per chunk is enough: XRPL puts every
    # transaction in exactly one ledger, so with no duplicate ledgers (checked above) a duplicate
    # hash could only occur inside one chunk.
    local = threading.local()

    def check_chunk(name: str) -> tuple:
        if not hasattr(local, "con"):  # one single-threaded connection per worker: 2 CPU threads in total
            local.con = duckdb.connect()
            local.con.execute("SET threads = 1; SET memory_limit = '2GB'")
        return local.con.execute(f"""
            WITH t AS (SELECT ledger_index, hash, fee_drops, result FROM read_parquet('{root}/transactions/{name}'))
            SELECT (SELECT count(*) FROM t), (SELECT count(DISTINCT hash) FROM t),
                   (SELECT coalesce(sum(fee_drops), 0) FROM t WHERE ledger_index > {lo}),
                   (SELECT count(*) FROM t WHERE result = 'tecINSUFF_FEE'),
                   (SELECT count(*) FROM read_parquet('{root}/ledgers/{name}') l
                    LEFT JOIN (SELECT ledger_index, count(*) n FROM t GROUP BY 1) c USING (ledger_index)
                    WHERE l.tx_count <> coalesce(c.n, 0))""").fetchone()

    n_tx = n_hash = mismatched = fees = insuff = 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        for i, (rows, hashes, fee, ins, bad) in enumerate(pool.map(check_chunk, names), 1):
            n_tx, n_hash, fees, insuff, mismatched = n_tx + rows, n_hash + hashes, fees + fee, insuff + ins, mismatched + bad
            if i % 1000 == 0:
                print(f"  ... {i:,}/{len(names):,} chunks checked", file=sys.stderr, flush=True)
    gaps = duckdb.sql(f"""
        SELECT ledger_index + 1 AS gap_start, next - 1 AS gap_end FROM (
          SELECT ledger_index, lead(ledger_index) OVER (ORDER BY ledger_index) AS next FROM {lg})
        WHERE next - ledger_index > 1 LIMIT 5""").fetchall()

    checks = [
        ("chunk pairs", tx_names == lg_names, f"{len(names)} paired chunks; unpaired: {sorted(tx_names ^ lg_names)[:5]}"),
        ("no gaps", n_distinct == hi - lo + 1, f"ledgers {lo:,}–{hi:,}; missing ranges: {gaps}"),
        ("no duplicate ledgers", n_led == n_distinct, f"{n_led:,} rows, {n_distinct:,} distinct"),
        ("no duplicate transactions", n_tx == n_hash, f"{n_tx:,} rows, {n_hash:,} distinct hashes"),
        ("tx_count matches rows", mismatched == 0, f"{mismatched} ledgers disagree"),
        # Burn between first and last ledger = fees of every ledger after the first (if the range is contiguous),
        # except that a tecINSUFF_FEE transaction burns only what its account holds: burn may be lower, never higher.
        ("burn equals fees", not gaps and (burned == fees or (burned < fees and insuff > 0)),
         f"burned {burned / 1e6:,.6f} XRP, fees {(fees or 0) / 1e6:,.6f} XRP; {insuff:,} tecINSUFF_FEE txs"),
    ]
    width = max(len(c[0]) for c in checks)
    for name, ok, detail in checks:
        print(f"{'OK  ' if ok else 'FAIL'} {name:<{width}}  {detail}")
    sys.exit(0 if all(ok for _, ok, _ in checks) else 1)


if __name__ == "__main__":
    main()
