"""Write a Data Package v2 descriptor (datapackage.json) for the Parquet export.

    uv run make_datapackage.py [--data /local/data/xrpl] [--with-graph]

Data Package is the Open Knowledge Foundation's open standard for describing datasets
(https://datapackage.org). Each Parquet directory becomes one multipart resource with a
Table Schema; checksums go to SHA256SUMS. Rerun after every export to refresh file lists,
sizes and coverage.
"""

import argparse
import hashlib
import time
import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb

NETWORK = "XRP Ledger mainnet"
SOURCES = [
    {"title": "Ripple full-history servers (s2.ripple.com)", "path": "https://s2.ripple.com:51234"},
    {"title": "XRPL Cluster (xrplcluster.com)", "path": "https://xrplcluster.com"},
]

F = lambda name, type_, desc, **kw: {"name": name, "type": type_, "description": desc, **kw}
LEDGER_FIELDS = [
    F("ledger_index", "integer", "Ledger sequence number.", constraints={"required": True, "unique": True}),
    F("ledger_hash", "string", "Ledger hash (hex)."),
    F("parent_hash", "string", "Hash of the previous ledger (hex)."),
    F("close_time", "datetime", "Ledger close time, UTC (second precision, rounded by close_time_resolution)."),
    F("close_time_resolution", "integer", "Close-time rounding in seconds."),
    F("total_drops", "integer", "Total XRP in existence after this ledger, in drops (1 XRP = 1,000,000 drops). Decreases as fees are burned."),
    F("tx_count", "integer", "Number of transactions in the ledger."),
    F("account_hash", "string", "Root hash of the ledger state tree (hex)."),
    F("transaction_hash", "string", "Root hash of the transaction tree (hex)."),
]
TX_FIELDS = [
    F("ledger_index", "integer", "Ledger that includes the transaction.", constraints={"required": True}),
    F("close_time", "datetime", "Close time of that ledger, UTC."),
    F("tx_index", "integer", "Position of the transaction within its ledger (metadata TransactionIndex)."),
    F("hash", "string", "Transaction hash (hex).", constraints={"required": True, "unique": True}),
    F("tx_type", "string", "TransactionType, e.g. Payment, OfferCreate, TrustSet."),
    F("account", "string", "Sending account (classic r-address)."),
    F("destination", "string", "Destination account, when the transaction type has one."),
    F("destination_tag", "integer", "DestinationTag, when present."),
    F("sequence", "integer", "Account sequence number (0 when a Ticket is used)."),
    F("fee_drops", "integer", "Fee paid, in drops. Burned, not paid to anyone."),
    F("result", "string", "Engine result, e.g. tesSUCCESS or tecPATH_DRY. tec* results are included in the ledger and still pay the fee."),
    F("delivered_drops", "integer", "Native XRP actually delivered, in drops (null for issued currencies)."),
    F("delivered_currency", "string", "Issued-currency code delivered (3-letter or 40-hex), null for XRP."),
    F("delivered_issuer", "string", "Issuer of the delivered currency."),
    F("delivered_value", "string", "Delivered issued-currency amount as a decimal string (kept as text to preserve precision)."),
    F("affected_nodes", "integer", "Number of ledger objects the transaction created, modified or deleted."),
    F("tx_json", "string", "Full transaction as returned by the ledger RPC method, JSON-encoded."),
    F("meta_json", "string", "Full transaction metadata, JSON-encoded."),
]

# Derived transfer-graph tables (gold), added only with --with-graph: publishing them is an open decision
# (PRD: XRPL Transfer Graph Analysis). Closed months only; schemas come from the Parquet files.
GRAPH_TABLES = {
    "transfer_edges_monthly": ("Transfer edges, monthly", ["month", "from_account", "to_account", "asset"],
        "One row per (month, sender, receiver, asset): successful Payment, CheckCash and EscrowFinish transfers "
        "summed (delivered amounts; self-transfers left out; MPT payments under asset 'MPT')."),
    "graph_entities": ("Entities", ["month", "account"],
        "Each account's entity for the month: accounts grouped only on on-ledger evidence (an issuer and its "
        "distribution wallet; small funding trees that transact mostly among themselves). Groups are evidence, not identity."),
    "graph_entity_edges": ("Entity edges, monthly", ["month", "from_entity", "to_entity", "asset"],
        "Transfer edges summed to entity level; transfers inside an entity are left out."),
    "graph_nodes": ("Graph metrics per entity", ["month", "asset", "entity_id"],
        "Degrees, transfers, value, PageRank, components, bow-tie part, Flow Index, community, Hodge potential and role, per asset ('*' = all assets, counts only)."),
    "graph_communities": ("Communities", ["month", "asset", "community"], "Leiden communities with at least 5 entities and their largest outgoing flows."),
    "graph_cycles": ("Cycles", ["month", "asset", "cycle_id"], "Strongest 2- and 3-cycles of transfers between entities."),
    "graph_summary": ("Graph summary", ["month", "asset"],
        "Per month and asset: size, tail fits, giant component, bow-tie shares, reciprocity, modularity, Hodge loop share, churn."),
}
PARQUET_TYPES = {"BIGINT": "integer", "INTEGER": "integer", "DOUBLE": "number", "VARCHAR": "string", "DATE": "date",
                 "BOOLEAN": "boolean"}


def graph_resources(root: Path, cache, sums: list) -> list:
    out = []
    open_month = datetime.now(timezone.utc).strftime("%Y-%m")
    for table, (title, pk, desc) in GRAPH_TABLES.items():
        files = sorted(f"gold/{table}/{f.name}" for f in (root / "gold" / table).glob("month=*.parquet")
                       if f.stem.split("=", 1)[1] < open_month)
        if not files:
            continue
        for p in files:
            digest = cache.get(root, p)
            if digest is None:
                cache.save()
                raise SystemExit(f"partial: hashed {cache.hashed} files this run; rerun to continue")
            sums.append(f"{digest}  {p}")
        cols = duckdb.sql(f"DESCRIBE SELECT * FROM read_parquet('{root}/{files[-1]}')").fetchall()
        fields = [{"name": c[0], "type": PARQUET_TYPES.get(c[1], "array" if c[1].endswith("[]") else "any")} for c in cols]
        out.append({"$schema": "https://datapackage.org/profiles/2.0/dataresource.json", "name": table.replace("_", "-"),
                    "title": title, "description": desc + " Derived; see README.md and the graph_version column.",
                    "type": "table", "path": files, "format": "parquet", "mediatype": "application/vnd.apache.parquet",
                    "schema": {"$schema": "https://datapackage.org/profiles/2.0/tableschema.json", "fields": fields,
                               "primaryKey": pk, "missingValues": ["", "nan"]}})
    return out


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class HashCache:
    """SHA-256 per file, cached by (size, mtime) in pipeline/sha256_cache.json: bronze files are never
    rewritten, so a refresh only hashes new files. Hashing all ~350 GB takes about an hour on the HDDs;
    with --max-seconds the work is split over several runs, each one saving what it hashed."""

    def __init__(self, root: Path, max_seconds: float | None):
        self.path = root / "pipeline" / "sha256_cache.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.deadline = time.time() + max_seconds if max_seconds else None
        self.hashed = 0

    def get(self, root: Path, rel: str) -> str | None:
        st = (root / rel).stat()
        hit = self.data.get(rel)
        if hit and hit[0] == st.st_size and hit[1] == st.st_mtime:
            return hit[2]
        if self.deadline and time.time() > self.deadline:
            return None
        self.data[rel] = [st.st_size, st.st_mtime, sha256(root / rel)]
        self.hashed += 1
        if self.hashed % 200 == 0:
            self.save()
        return self.data[rel][2]

    def save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data))
        tmp.replace(self.path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=Path("/local/data/xrpl"))
    ap.add_argument("--max-seconds", type=float, help="stop hashing after this long; rerun to continue")
    ap.add_argument("--with-graph", action="store_true", help="also describe the derived transfer-graph tables (closed months)")
    args = ap.parse_args()
    root = args.data
    cache = HashCache(root, args.max_seconds)

    names = sorted(f.name for f in (root / "transactions").glob("*.parquet") if (root / "ledgers" / f.name).exists())
    if not names:
        raise SystemExit(f"no complete chunks under {root}")
    lg = "read_parquet([" + ",".join(f"'{root}/ledgers/{n}'" for n in names) + "])"
    duckdb.sql("SET TimeZone = 'UTC'")
    first, last, n, t0, t1 = duckdb.sql(f"""
        SELECT min(ledger_index), max(ledger_index), count(*),
               strftime(min(close_time), '%Y-%m-%dT%H:%M:%SZ'), strftime(max(close_time), '%Y-%m-%dT%H:%M:%SZ')
        FROM {lg}""").fetchone()
    ntx = duckdb.sql("SELECT count(*) FROM read_parquet([" + ",".join(f"'{root}/transactions/{n}'" for n in names) + "])").fetchone()[0]

    sums, resources = [], []
    for kind, fields, pk, title in [
        ("ledgers", LEDGER_FIELDS, ["ledger_index"], "Ledgers"),
        ("transactions", TX_FIELDS, ["hash"], "Transactions"),
    ]:
        paths = [f"{kind}/{n}" for n in names]
        for p in paths:
            digest = cache.get(root, p)
            if digest is None:
                cache.save()
                left = sum(1 for k in ("ledgers", "transactions") for n in names if f"{k}/{n}" not in cache.data)
                raise SystemExit(f"partial: hashed {cache.hashed} files this run, about {left} left; rerun to continue")
            sums.append(f"{digest}  {p}")
        schema = {"$schema": "https://datapackage.org/profiles/2.0/tableschema.json", "fields": fields, "primaryKey": pk,
                  # Empty cells are Parquet nulls; "nan" covers readers that load nullable integers through pandas.
                  "missingValues": ["", "nan"]}
        if kind == "transactions":
            schema["foreignKeys"] = [{"fields": ["ledger_index"], "reference": {"resource": "ledgers", "fields": ["ledger_index"]}}]
        resources.append({
            "$schema": "https://datapackage.org/profiles/2.0/dataresource.json",
            "name": kind,
            "title": title,
            "description": f"One row per {kind[:-1]}. Split into chunks of up to 1,000 ledgers; the file name is the ledger range.",
            "type": "table",
            "path": paths,
            "format": "parquet",
            "mediatype": "application/vnd.apache.parquet",
            "schema": schema,
        })
    if args.with_graph:
        resources += graph_resources(root, cache, sums)
    cache.save()
    (root / "SHA256SUMS").write_text("\n".join(sums) + "\n")

    pkg = {
        "$schema": "https://datapackage.org/profiles/2.0/datapackage.json",
        "name": "xrpl-mainnet-ledgers",
        "title": f"XRP Ledger mainnet ledgers and transactions, #{first:,} to #{last:,}",
        "description": (
            f"Every ledger and transaction of the {NETWORK} between ledger {first} ({t0}) and {last} ({t1}): "
            f"{n:,} ledgers and {ntx:,} transactions, with the full raw transaction and metadata JSON.\n\n"
            "Exported from public full-history servers by the XRPL Open Data Project with `export_ledgers.py`. "
            "Stored as zstd-compressed Parquet; query directly with DuckDB, pandas, Polars, R or Spark. "
            "Integrity check: the drop in `total_drops` between two ledgers equals the fees burned in between. "
            "See README.md for usage and known limitations."
        ),
        "version": "0.1.0",
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "keywords": ["XRP Ledger", "XRPL", "blockchain", "distributed ledger", "transactions", "DEX", "open data"],
        "licenses": [{"name": "CC-BY-4.0", "path": "https://creativecommons.org/licenses/by/4.0/",
                      "title": "Creative Commons Attribution 4.0"}],
        "contributors": [{"title": "XRPL Open Data Project", "email": "otaviovacari@usp.br", "roles": ["creator"]}],
        "sources": SOURCES,
        "temporalCoverage": {"start": t0, "end": t1, "ledgers": [first, last]},
        "resources": resources,
    }
    (root / "datapackage.json").write_text(json.dumps(pkg, indent=2) + "\n")
    print(f"wrote {root}/datapackage.json ({n:,} ledgers, {ntx:,} txs, {len(names)} chunks) and SHA256SUMS")


if __name__ == "__main__":
    main()
