"""Table registry: every silver and gold table, its SQL file and what it reads.

Silver tables are built per bronze chunk, in this order. `nodes` means the table reads the shared
exploded meta_json.AffectedNodes view; `pool_deltas` the shared AMM balance-change view.

Gold tables:
  daily   -> gold/<name>/day=YYYY-MM-DD.parquet, recomputed for the days a run touched
  monthly -> gold/<name>/month=YYYY-MM.parquet, recomputed for the months a run touched
  running -> gold/<name>.parquet, merged with the rows of newly processed chunks only
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Silver:
    name: str
    nodes: bool = False
    pool_deltas: bool = False
    # Optional SQL predicate on bronze rows: the only transactions that can produce this table's rows.
    # When every table in a run has one, `nodes` is built from the matching transactions only (much faster).
    tx_filter: str | None = None


@dataclass(frozen=True)
class Gold:
    name: str
    kind: str                      # daily | monthly | running
    reads: tuple[str, ...] = ()    # silver tables
    ledgers: bool = False          # also reads bronze ledgers (with one day of neighbours)


SILVER = [
    Silver("transactions_base"),
    Silver("transfers", nodes=True),
    Silver("dex_swaps", nodes=True, pool_deltas=True),
    Silver("amm_events", nodes=True, pool_deltas=True),
    # NFTokenPage and NFTokenOffer objects change only in NFToken* transactions.
    Silver("nft_events", nodes=True, tx_filter="starts_with(tx_type, 'NFToken')"),
    Silver("trustline_changes", nodes=True),
    Silver("account_lifecycle", nodes=True),
]

GOLD = [
    Gold("network_daily", "daily", ("transactions_base",), ledgers=True),
    Gold("network_hourly", "daily", (), ledgers=True),
    Gold("close_gaps_daily", "daily", (), ledgers=True),
    Gold("network_monthly", "monthly", ("transactions_base",)),
    Gold("dex_ohlcv_daily", "daily", ("dex_swaps",)),
    Gold("amm_daily", "daily", ("dex_swaps", "amm_events")),
    Gold("token_daily", "daily", ("transfers", "trustline_changes")),
    Gold("payment_size_daily", "daily", ("transfers",)),
    Gold("nft_daily", "daily", ("nft_events",)),
    Gold("anomalies_daily", "daily", ("transactions_base",)),
    Gold("account_monthly", "monthly", ("transactions_base",)),
    Gold("dest_tag_monthly", "monthly", ("transfers",)),
    Gold("transfer_edges_monthly", "monthly", ("transfers",)),
    Gold("account_first_seen", "running", ("transactions_base", "account_lifecycle")),
    Gold("records", "running", ("transactions_base", "transfers"), ledgers=True),
]

SILVER_BY_NAME = {t.name: t for t in SILVER}
GOLD_BY_NAME = {t.name: t for t in GOLD}
