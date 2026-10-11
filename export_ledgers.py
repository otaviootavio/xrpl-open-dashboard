"""Export XRPL ledgers (with full transactions) to Parquet.

Examples:
    uv run export_ledgers.py --hours 24
    uv run export_ledgers.py --start 107200000 --end 107209999

Output (under --out, default /local/data/xrpl):
    ledgers/<first>-<last>.parquet        one row per ledger
    transactions/<first>-<last>.parquet   one row per transaction (incl. raw tx + metadata JSON)
    runs/<utc-timestamp>.json             what was requested and where it came from

Work is split into fixed chunks aligned to --chunk (default 1000 ledgers). A chunk is
written atomically once all of its ledgers are fetched, and chunks already on disk are
skipped, so an interrupted run can simply be restarted with the same arguments.
"""

import argparse
import asyncio
import hashlib
import json
import random
import struct
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.parquet as pq

# (url, max concurrent requests): public mainnet endpoints that served full history when probed on
# 2026-09-27. The dispatcher loop in main() sends each request to the endpoint with the lowest expected
# wait and adapts each endpoint's concurrency between 1 and this cap: +1 per `limit` successes, halved
# and paused on throttling. xrplcluster.com and xrpl.link throttle hard, so their caps stay low.
# Left out: xrpl.ws (same cluster as xrplcluster.com), Tatum (recent history only), Uniblock and dRPC.
ENDPOINTS = [
    ("https://s2.ripple.com:51234", 8),       # Ripple, full-history cluster
    ("https://s1.ripple.com:51234", 6),       # Ripple, general purpose (reports 32570-current)
    ("https://honeycluster.io", 6),           # Honeycluster, full history via Clio
    ("https://rpc.ankr.com/xrp_mainnet", 6),  # Ankr free tier
    ("https://xrplcluster.com", 2),           # InFTF, throttles hard
    ("https://xrpl.link", 2),                 # reports 32570-current, throttles hard
]
MAX_IN_FLIGHT = 24  # politeness cap on requests in flight across all endpoints
WINDOW = 2          # chunks fetched at once, so the next chunk keeps workers busy while one finishes
MAX_ATTEMPTS = 30   # a ledger that fails this often, across all endpoints, stops the run
RIPPLE_EPOCH = 946684800  # 2000-01-01T00:00:00Z
RETRYABLE = {"tooBusy", "slowDown", "noNetwork", "noCurrent", "noClosed", "lgrNotFound", "internal"}

LEDGER_SCHEMA = pa.schema([
    ("ledger_index", pa.int64()),
    ("ledger_hash", pa.string()),
    ("parent_hash", pa.string()),
    ("close_time", pa.timestamp("s", tz="UTC")),
    ("close_time_resolution", pa.int32()),
    ("total_drops", pa.int64()),
    ("tx_count", pa.int32()),
    ("account_hash", pa.string()),
    ("transaction_hash", pa.string()),
])

TX_SCHEMA = pa.schema([
    ("ledger_index", pa.int64()),
    ("close_time", pa.timestamp("s", tz="UTC")),
    ("tx_index", pa.int32()),
    ("hash", pa.string()),
    ("tx_type", pa.string()),
    ("account", pa.string()),
    ("destination", pa.string()),
    ("destination_tag", pa.int64()),
    ("sequence", pa.int64()),
    ("fee_drops", pa.int64()),
    ("result", pa.string()),
    ("delivered_drops", pa.int64()),       # native XRP delivered (Payments etc.)
    ("delivered_currency", pa.string()),   # issued-currency delivered amount
    ("delivered_issuer", pa.string()),
    ("delivered_value", pa.string()),      # kept as string: IOU amounts exceed float precision
    ("affected_nodes", pa.int32()),
    ("tx_json", pa.string()),
    ("meta_json", pa.string()),
])


class RPC:
    def __init__(self, url: str, client: httpx.AsyncClient):
        self.url, self.client = url, client
        self.paused_until = 0.0  # set when the endpoint throttles us; all its workers wait

    def pause(self, seconds: float) -> None:
        self.paused_until = max(self.paused_until, time.monotonic() + seconds)

    async def call(self, method: str, params: dict) -> dict:
        """One JSON-RPC call; raises RuntimeError(code) on retryable failures."""
        r = await self.client.post(self.url, json={"method": method, "params": [params]})
        if r.status_code in (429, 502, 503, 504):
            raise RuntimeError(f"http{r.status_code}")
        r.raise_for_status()
        res = r.json().get("result", {})
        if res.get("status") == "error" or "error" in res:
            raise RuntimeError(res.get("error", "unknown"))
        return res


class Endpoint:
    """Dispatcher-side state of one endpoint: adaptive concurrency limit, pause and latency estimate."""

    def __init__(self, rpc: RPC, cap: int):
        self.rpc, self.cap = rpc, cap
        self.limit, self.in_flight, self.latency, self.streak = 2.0, 0, 1.0, 0
        self.ok = self.errors = 0

    def ready(self, now: float) -> bool:
        return now >= self.rpc.paused_until and self.in_flight < int(self.limit)

    def expected_wait(self) -> float:
        return self.latency * (self.in_flight + 1) / int(self.limit)

    def succeeded(self, seconds: float) -> None:
        self.ok += 1
        self.streak = 0
        self.latency = 0.8 * self.latency + 0.2 * seconds
        self.limit = min(self.cap, self.limit + 1 / self.limit)

    def failed(self) -> None:
        self.errors += 1
        self.streak += 1
        self.limit = max(1.0, self.limit / 2)
        self.rpc.pause(min(2 ** self.streak, 60) + random.random())


def to_ts(ripple_time: int) -> datetime:
    return datetime.fromtimestamp(ripple_time + RIPPLE_EPOCH, tz=timezone.utc)


def header_ok(l: dict) -> bool:
    """True if the header fields hash to ledger_hash (SHA-512Half over the 'LWR' prefix + header)."""
    b = (b"LWR\x00" + struct.pack(">IQ", int(l["ledger_index"]), int(l["total_coins"]))
         + bytes.fromhex(l["parent_hash"]) + bytes.fromhex(l["transaction_hash"]) + bytes.fromhex(l["account_hash"])
         + struct.pack(">IIBB", l["parent_close_time"], l["close_time"], l["close_time_resolution"], l["close_flags"]))
    return hashlib.sha512(b).hexdigest()[:64].upper() == l["ledger_hash"]


def ledger_row(l: dict) -> dict:
    return {
        "ledger_index": int(l["ledger_index"]),
        "ledger_hash": l["ledger_hash"],
        "parent_hash": l["parent_hash"],
        "close_time": to_ts(l["close_time"]),
        "close_time_resolution": l["close_time_resolution"],
        "total_drops": int(l["total_coins"]),
        "tx_count": len(l.get("transactions", [])),
        "account_hash": l["account_hash"],
        "transaction_hash": l["transaction_hash"],
    }


def tx_rows(l: dict) -> list[dict]:
    rows, idx, ct = [], int(l["ledger_index"]), to_ts(l["close_time"])
    for tx in l.get("transactions", []):
        meta = tx.pop("metaData", None) or tx.pop("meta", {})
        d = meta.get("delivered_amount")
        rows.append({
            "ledger_index": idx,
            "close_time": ct,
            "tx_index": meta.get("TransactionIndex"),
            "hash": tx.get("hash"),
            "tx_type": tx.get("TransactionType"),
            "account": tx.get("Account"),
            "destination": tx.get("Destination"),
            "destination_tag": tx.get("DestinationTag"),
            "sequence": tx.get("Sequence"),
            "fee_drops": int(tx["Fee"]) if "Fee" in tx else None,
            "result": meta.get("TransactionResult"),
            "delivered_drops": int(d) if isinstance(d, str) and d.isdigit() else None,
            "delivered_currency": d.get("currency") if isinstance(d, dict) else None,
            "delivered_issuer": d.get("issuer") if isinstance(d, dict) else None,
            "delivered_value": d.get("value") if isinstance(d, dict) else None,
            "affected_nodes": len(meta.get("AffectedNodes", [])),
            "tx_json": json.dumps(tx, separators=(",", ":")),
            "meta_json": json.dumps(meta, separators=(",", ":")),
        })
    return rows


def stored_ledger(ledgers_dir: Path, index: int) -> dict | None:
    """Hashes of ledger `index` from a chunk already on disk, or None if no chunk holds it."""
    for f in ledgers_dir.glob("*.parquet"):
        lo, hi = map(int, f.stem.split("-"))
        if lo <= index <= hi:
            t = pq.read_table(f, columns=["ledger_index", "ledger_hash", "parent_hash"],
                              filters=[("ledger_index", "=", index)])
            return t.to_pylist()[0] if t.num_rows else None
    return None


def write_atomic(table: pa.Table, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp, compression="zstd", compression_level=9)
    tmp.rename(path)


async def ledger_at(rpc: RPC, when: datetime, lo: int, hi: int) -> int:
    """First ledger whose close time is >= `when` (binary search on headers)."""
    while lo < hi:
        mid = (lo + hi) // 2
        l = (await rpc.call("ledger", {"ledger_index": mid}))["ledger"]
        if to_ts(l["close_time"]) < when:
            lo = mid + 1
        else:
            hi = mid
    return lo


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, help="export the last N hours of validated ledgers")
    ap.add_argument("--start", type=int, help="first ledger index")
    ap.add_argument("--end", type=int, help="last ledger index (default: latest validated)")
    ap.add_argument("--out", type=Path, default=Path("/local/data/xrpl"))
    ap.add_argument("--chunk", type=int, default=1000, help="ledgers per Parquet file")
    ap.add_argument("--newest-first", action="store_true",
                    help="fetch chunks from the end of the range backwards (for backfills)")
    args = ap.parse_args()
    if args.hours is None and args.start is None:
        ap.error("give --hours or --start")

    limits = httpx.Limits(max_connections=sum(n for _, n in ENDPOINTS) + 4)
    async with httpx.AsyncClient(timeout=60, limits=limits, headers={"User-Agent": "xrpl-open-data/0.1"}) as client:
        rpcs = [(RPC(url, client), n) for url, n in ENDPOINTS]
        primary = rpcs[0][0]

        end = args.end or int((await primary.call("ledger", {"ledger_index": "validated"}))["ledger"]["ledger_index"])
        if args.start is not None:
            start = args.start
        else:
            now = to_ts((await primary.call("ledger", {"ledger_index": end}))["ledger"]["close_time"])
            guess = end - int(args.hours * 3600 / 3.0)  # ledgers close every ~3-4 s; 3 s overshoots safely
            start = await ledger_at(primary, now - timedelta(hours=args.hours), guess, end)
        print(f"Range {start:,} .. {end:,} ({end - start + 1:,} ledgers) -> {args.out}")

        # Chunks aligned to --chunk boundaries, clipped to the requested range.
        chunks = []
        for c0 in range(start - start % args.chunk, end + 1, args.chunk):
            a, b = max(c0, start), min(c0 + args.chunk - 1, end)
            name = f"{a:010d}-{b:010d}.parquet"
            if not (args.out / "transactions" / name).exists():
                chunks.append((a, b, name))
        if args.newest_first:
            chunks.reverse()
        todo = sum(b - a + 1 for a, b, _ in chunks)
        print(f"{len(chunks)} chunks to fetch ({todo:,} ledgers); skipped chunks already on disk")

        run = {"started": datetime.now(timezone.utc).isoformat(), "start": start, "end": end,
               "endpoints": [u for u, _ in ENDPOINTS], "chunk": args.chunk, "chunks_written": []}
        stats = {"done": 0, "tx": 0, "retries": 0, "rejected": 0,
                 "by_endpoint": {u: 0 for u, _ in ENDPOINTS}, "errors_by_endpoint": {u: 0 for u, _ in ENDPOINTS}}
        t0 = time.time()

        eps = [Endpoint(r, n) for r, n in rpcs]

        def write_chunk(a: int, b: int, name: str, got: dict[int, tuple[dict, list[dict]]]) -> int:
            rows = [got[i] for i in range(a, b + 1)]
            ledgers = [lr for lr, _ in rows]
            for prev, cur in zip(ledgers, ledgers[1:]):
                if cur["parent_hash"] != prev["ledger_hash"]:
                    raise SystemExit(f"Broken hash chain at ledger {cur['ledger_index']}; chunk {name} not written.")
            # Link the chunk to its neighbours on disk, so the stored chain has no silent breaks.
            before, after = stored_ledger(args.out / "ledgers", a - 1), stored_ledger(args.out / "ledgers", b + 1)
            if before and before["ledger_hash"] != ledgers[0]["parent_hash"]:
                raise SystemExit(f"Ledger {a} does not link to stored ledger {a - 1}; chunk {name} not written.")
            if after and after["parent_hash"] != ledgers[-1]["ledger_hash"]:
                raise SystemExit(f"Stored ledger {b + 1} does not link to ledger {b}; chunk {name} not written.")
            txs = [t for _, ts in rows for t in ts]
            write_atomic(pa.Table.from_pylist(ledgers, LEDGER_SCHEMA), args.out / "ledgers" / name)
            write_atomic(pa.Table.from_pylist(txs, TX_SCHEMA), args.out / "transactions" / name)
            return len(txs)

        async def fetch_one(ep: Endpoint, i: int) -> tuple[dict, list[dict]]:
            """One request to one endpoint; returns the ledger already converted to rows."""
            l = (await ep.rpc.call("ledger", {"ledger_index": i, "transactions": True, "expand": True}))["ledger"]
            if int(l["ledger_index"]) != i or not header_ok(l):
                raise RuntimeError("badHeader")
            return ledger_row(l), tx_rows(l)

        # Dispatcher: owns the queue, picks an endpoint per request, and duplicates a request that
        # holds up the oldest chunk for too long (the first answer wins).
        pending: deque[int] = deque()
        active: list[tuple[int, int, str, dict]] = []   # chunks being fetched, in write order
        chunk_of: dict[int, dict] = {}                   # ledger -> its chunk's result dict
        tasks: dict[asyncio.Task, tuple[Endpoint, int, float]] = {}
        serving: dict[int, set[Endpoint]] = {}           # ledger -> endpoints it is in flight on
        todo_chunks = iter(chunks)
        attempts: dict[int, int] = {}
        seen_errors: set[str] = set()
        k = 0
        while True:
            while len(active) < WINDOW and (c := next(todo_chunks, None)):
                a, b, name = c
                got: dict = {}
                active.append((a, b, name, got))
                pending.extend(range(a, b + 1))
                chunk_of.update((i, got) for i in range(a, b + 1))
            if not active:
                break
            now = time.monotonic()
            while len(tasks) < MAX_IN_FLIGHT:
                i = None
                while pending and i is None:
                    j = pending.popleft()
                    if j not in chunk_of[j] and not serving.get(j):
                        i = j
                if i is None:  # queue drained: hedge the oldest chunk's slowest ledger
                    a0, b0, _, got0 = active[0]
                    median = sorted(e.latency for e in eps)[len(eps) // 2]
                    slow = [(sent, j) for _, j, sent in tasks.values()
                            if chunk_of.get(j) is got0 and len(serving[j]) == 1 and now - sent > max(3 * median, 3)]
                    if not slow:
                        break
                    i = min(slow)[1]
                ready = [e for e in eps if e.ready(now) and e not in serving.get(i, ())]
                if not ready:
                    if not serving.get(i) and i not in chunk_of[i]:
                        pending.appendleft(i)
                    break
                ep = min(ready, key=Endpoint.expected_wait)
                ep.in_flight += 1
                serving.setdefault(i, set()).add(ep)
                tasks[asyncio.create_task(fetch_one(ep, i))] = (ep, i, now)
            if not tasks:  # every endpoint is paused
                await asyncio.sleep(max(0.05, min(e.rpc.paused_until for e in eps) - time.monotonic()))
                continue
            done, _ = await asyncio.wait(tasks, timeout=1, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                ep, i, sent = tasks.pop(t)
                ep.in_flight -= 1
                if i not in chunk_of:  # a duplicate answering after its chunk was written
                    if not t.cancelled() and t.exception() is None:
                        ep.succeeded(time.monotonic() - sent)
                    continue
                serving[i].discard(ep)
                try:
                    result = t.result()
                    ep.succeeded(time.monotonic() - sent)
                    chunk_of[i].setdefault(i, result)
                except (RuntimeError, KeyError, ValueError, httpx.HTTPError) as e:
                    # Any error counts against the endpoint and the ledger is retried elsewhere; with
                    # several servers, one server's odd error (e.g. Clio's failedToForward) is not fatal.
                    code = str(e) or type(e).__name__
                    if code not in RETRYABLE and not code.startswith("http") and code not in seen_errors:
                        seen_errors.add(code)
                        print(f"    new error {code!r} from {ep.rpc.url} on ledger {i}; retrying elsewhere", flush=True)
                    attempts[i] = attempts.get(i, 0) + 1
                    if attempts[i] >= MAX_ATTEMPTS:
                        raise SystemExit(f"Ledger {i} failed {attempts[i]} times (last: {code} from {ep.rpc.url}); stopping.")
                    stats["rejected"] += code == "badHeader"
                    stats["retries"] += 1
                    ep.failed()
                    if i not in chunk_of[i] and not serving[i]:
                        pending.appendleft(i)
            while active and len(active[0][3]) == active[0][1] - active[0][0] + 1:
                a, b, name, got = active.pop(0)
                ntx = await asyncio.to_thread(write_chunk, a, b, name, got)  # requests keep flowing meanwhile
                for i in range(a, b + 1):
                    del chunk_of[i]
                    serving.pop(i, None)
                    attempts.pop(i, None)
                run["chunks_written"].append(name)
                stats["done"] += b - a + 1
                stats["tx"] += ntx
                k += 1
                rate = stats["done"] / (time.time() - t0)
                eta = (todo - stats["done"]) / rate if rate else 0
                print(f"  {name}  {ntx:>7,} txs | {stats['done']:,}/{todo:,} ledgers  {rate:.1f}/s  "
                      f"retries {stats['retries']}  ETA {eta / 60:.0f} min", flush=True)
                if k % 50 == 0 or k == len(chunks):
                    for e in eps:
                        print(f"    {e.ok:>9,} ok  {e.errors:>7,} errors  limit {int(e.limit)}/{e.cap}  "
                              f"{e.latency:.2f} s  {e.rpc.url}", flush=True)
        for t in tasks:  # leftover duplicate requests
            t.cancel()
        stats["by_endpoint"] = {e.rpc.url: e.ok for e in eps}
        stats["errors_by_endpoint"] = {e.rpc.url: e.errors for e in eps}

        run.update(finished=datetime.now(timezone.utc).isoformat(), ledgers=stats["done"],
                   transactions=stats["tx"], retries=stats["retries"], rejected_headers=stats["rejected"],
                   by_endpoint=stats["by_endpoint"], errors_by_endpoint=stats["errors_by_endpoint"])
        (args.out / "runs").mkdir(parents=True, exist_ok=True)
        (args.out / "runs" / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json").write_text(json.dumps(run, indent=2))
        print(f"Done: {stats['done']:,} ledgers, {stats['tx']:,} txs in {(time.time() - t0) / 60:.1f} min")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit("Interrupted; rerun the same command to resume.")
