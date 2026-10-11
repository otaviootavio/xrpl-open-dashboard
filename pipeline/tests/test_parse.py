"""Parse-rule tests: run each silver SQL on real bronze transactions and compare with hand-checked values.

    uv run pytest pipeline/tests -q

Reads one bronze chunk from /local/data/xrpl (override with XRPL_DATA); writes nothing there.
"""

import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import run  # noqa: E402

DATA = Path(os.environ.get("XRPL_DATA", "/local/data/xrpl"))
FIX = json.loads((HERE / "fixtures.json").read_text())
CHUNK = DATA / "transactions" / FIX["chunk"]
pytestmark = pytest.mark.skipif(not CHUNK.exists(), reason=f"bronze chunk {CHUNK} not available")


@pytest.fixture(scope="module")
def con(tmp_path_factory):
    c = run.connect(run.Paths(tmp_path_factory.mktemp("pipeline")))
    c.execute(f"CREATE TEMP VIEW src AS SELECT * FROM read_parquet('{CHUNK}')")
    c.execute(f"CREATE TEMP TABLE nodes AS {(run.SQL / 'silver/_nodes.sql').read_text()}")
    c.execute(f"CREATE TEMP TABLE pool_deltas AS {(run.SQL / 'silver/_pool_deltas.sql').read_text()}")
    for t in run.SILVER:
        c.execute(f"CREATE TEMP TABLE out_{t.name} AS {(run.SQL / f'silver/{t.name}.sql').read_text()}")
    return c


def rows(con, table, where):
    cur = con.execute(f"SELECT * FROM out_{table} WHERE {where}")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


@pytest.mark.parametrize("fx", FIX["fixtures"], ids=lambda f: f"{f['table']}:{f['hash'][:8]}")
def test_fixture(con, fx):
    got = rows(con, fx["table"], f"hash = '{fx['hash']}' AND ({fx.get('where', 'true')})")
    if "rows" in fx:
        assert len(got) == fx["rows"], (fx["why"], got)
    if "min_rows" in fx:
        assert len(got) >= fx["min_rows"], (fx["why"], got)
    for r in got:
        for k, v in fx.get("expect", {}).items():
            assert r[k] == pytest.approx(v) if isinstance(v, float) else r[k] == v, (fx["why"], k, r)
        for k, v in fx.get("min", {}).items():
            assert r[k] > v, (fx["why"], k, r)
        for k, v in fx.get("max", {}).items():
            assert r[k] < v, (fx["why"], k, r)


def test_chunk_invariants(con):
    one = lambda sql: con.execute(sql).fetchone()[0]
    assert one("SELECT count(*) FROM out_transactions_base") == one("SELECT count(*) FROM src")
    # one minted token per successful NFTokenMint, one burned per NFTokenBurn
    assert one("SELECT count(*) FROM out_nft_events WHERE event = 'mint'") == \
        one("SELECT count(*) FROM src WHERE tx_type = 'NFTokenMint' AND result = 'tesSUCCESS'")
    assert one("SELECT count(*) FROM out_nft_events WHERE event = 'burn'") == \
        one("SELECT count(*) FROM src WHERE tx_type = 'NFTokenBurn' AND result = 'tesSUCCESS'")
    # XRP delivered by Payments = XRP in transfers for Payments
    assert one("SELECT sum(drops) FROM out_transfers WHERE tx_type = 'Payment' AND asset = 'XRP'") == \
        one("SELECT sum(delivered_drops) FROM src WHERE tx_type = 'Payment' AND result = 'tesSUCCESS'")
    assert one("SELECT count(*) FROM out_dex_swaps WHERE NOT (amount_out > 0 AND amount_in > 0)") == 0
    assert one("SELECT count(*) FROM out_account_lifecycle WHERE event = 'created'") == 118


BURN_CHUNK = DATA / "transactions" / "0103000000-0103000999.parquet"  # March 2026 NFT burn spike


@pytest.mark.skipif(not BURN_CHUNK.exists(), reason="burn-spike chunk not available")
def test_nft_burn_spike(tmp_path):
    """Page-diff burns and mints equal the successful NFTokenBurn / NFTokenMint transactions (13,895 / 69)."""
    c = run.connect(run.Paths(tmp_path))
    c.execute(f"CREATE TEMP VIEW src AS SELECT * FROM read_parquet('{BURN_CHUNK}')")
    c.execute(f"CREATE TEMP TABLE nodes AS {(run.SQL / 'silver/_nodes.sql').read_text()}")
    c.execute(f"CREATE TEMP TABLE out AS {(run.SQL / 'silver/nft_events.sql').read_text()}")
    one = lambda sql: c.execute(sql).fetchone()[0]
    for event, tx_type in (("burn", "NFTokenBurn"), ("mint", "NFTokenMint")):
        assert one(f"SELECT count(*) FROM out WHERE event = '{event}'") == \
            one(f"SELECT count(*) FROM src WHERE tx_type = '{tx_type}' AND result = 'tesSUCCESS'")
    assert one("SELECT count(*) FROM out WHERE event = 'burn'") == 13895


@pytest.mark.parametrize("chunk", [CHUNK, BURN_CHUNK], ids=["recent", "burn-spike"])
def test_nft_fast_path_identical(tmp_path, chunk):
    """Building `nodes` from NFToken* transactions only (tiers tx_filter) gives the same nft_events rows."""
    if not chunk.exists():
        pytest.skip("chunk not available")
    from tiers import SILVER_BY_NAME
    c = run.connect(run.Paths(tmp_path))
    c.execute(f"CREATE TEMP VIEW src AS SELECT * FROM read_parquet('{chunk}')")
    nodes = (run.SQL / "silver/_nodes.sql").read_text()
    sql = (run.SQL / "silver/nft_events.sql").read_text()
    c.execute(f"CREATE TEMP TABLE nodes AS {nodes}")
    c.execute(f"CREATE TEMP TABLE full_out AS {sql}")
    fast = nodes.replace("FROM src)", f"FROM src WHERE {SILVER_BY_NAME['nft_events'].tx_filter})")
    c.execute(f"CREATE OR REPLACE TEMP TABLE nodes AS {fast}")
    c.execute(f"CREATE TEMP TABLE fast_out AS {sql}")
    one = lambda q: c.execute(q).fetchone()[0]
    assert one("SELECT count(*) FROM full_out") == one("SELECT count(*) FROM fast_out")
    assert one("SELECT count(*) FROM (SELECT * FROM full_out EXCEPT ALL SELECT * FROM fast_out)") == 0
