"""Gold transfer_edges_monthly: edge direction per transfer type, partial payments, self-transfers, totals.

    uv run pytest pipeline/tests/test_edges.py -q

Builds silver transfers for two real bronze chunks (the fixture chunk, plus one with a third-party
EscrowFinish), runs the gold SQL over them and checks hand-verified transactions.
"""

import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import run  # noqa: E402

DATA = Path(os.environ.get("XRPL_DATA", "/local/data/xrpl"))
CHUNKS = [DATA / "transactions" / "0107281000-0107281933.parquet",   # fixture chunk (CheckCash, partials)
          DATA / "transactions" / "0107345000-0107345999.parquet"]   # EscrowFinish by a third party
pytestmark = pytest.mark.skipif(not all(c.exists() for c in CHUNKS), reason="bronze chunks not available")


@pytest.fixture(scope="module")
def con(tmp_path_factory):
    c = run.connect(run.Paths(tmp_path_factory.mktemp("edges")))
    for i, chunk in enumerate(CHUNKS):
        c.execute(f"CREATE OR REPLACE TEMP VIEW src AS SELECT * FROM read_parquet('{chunk}')")
        c.execute(f"CREATE OR REPLACE TEMP TABLE nodes AS {(run.SQL / 'silver/_nodes.sql').read_text()}")
        verb = "CREATE TEMP TABLE s_transfers AS" if i == 0 else "INSERT INTO s_transfers"
        c.execute(f"{verb} {(run.SQL / 'silver/transfers.sql').read_text()}")
    c.execute(f"CREATE TEMP TABLE edges AS {(run.SQL / 'gold/transfer_edges_monthly.sql').read_text()}")
    return c


def edge(con, frm, to, asset="XRP"):
    cur = con.execute("SELECT * FROM edges WHERE from_account = ? AND to_account = ? AND asset = ?", [frm, to, asset])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def test_checkcash_runs_from_check_owner_to_casher(con):
    # 016B3FF1...: r4Vop cashes a Check written by rPP3R (FinalFields.Account of the deleted Check)
    [e] = edge(con, "rPP3R33zpehB5hTy2jSYZek5ntq3HQym68", "r4VopBHDgjnnkdxYXhZ8Mwi6bavSvMgtde")
    assert e["n_checkcash"] >= 1 and e["n_transfers"] >= 2 and e["drops"] >= 701462000  # a Payment too


def test_escrowfinish_runs_from_owner_to_destination(con):
    # A8327C55...: rPw6 submits the EscrowFinish; the escrow is rfh8's, paying rKcE 350 XRP
    [e] = edge(con, "rfh8WPxsx4koNzeyQPVVaQLguHqGTLAVzv", "rKcE9TTuawkqnGiGvVz8wYfREFrTCpyW7r")
    assert e["n_escrowfinish"] == 1 and e["n_payment"] == 0 and e["drops"] == 350000000 and e["n_transfers"] == 1
    assert not edge(con, "rPw6JAdM9BGwH4y4LxJQBDybaGNhFLCspU", "rKcE9TTuawkqnGiGvVz8wYfREFrTCpyW7r")


def test_partial_payment_counted(con):
    # 00377F2F...: partial payment of Horizon tokens, delivered 0.03963482024
    [e] = edge(con, "rs1QjeNm5zJAKfPBvpTS4kzvdTNgAQ3VWi", "rHorizonVoYewL8GjL9rqfrj9QgHWyEm1H",
               "486F72697A6F6E00000000000000000000000000.rDKJWtrPqKiMBzK9FTVvebMVec7FJtJLx6")
    assert e["n_partial"] >= 1 and e["drops"] is None and e["value_f"] > 0


def test_self_transfers_left_out(con):
    one = lambda q: con.execute(q).fetchone()[0]
    assert one("SELECT count(*) FROM edges WHERE from_account = to_account") == 0
    assert not edge(con, "rogue5HnPRSszD9CWGSUz8UGHMVwSSKF6", "rogue5HnPRSszD9CWGSUz8UGHMVwSSKF6",
                    "504F57444552204B454700000000000000000000.rwrsUT6ynqimffkxd6tAohFRDGHgGFmFjc")


def test_totals_match_silver(con):
    one = lambda q: con.execute(q).fetchone()[0]
    assert one("SELECT count(*) FROM edges WHERE asset IS NULL OR value_f IS NULL") == 0
    assert one("SELECT sum(n_transfers) FROM edges") == one("SELECT count(*) FROM s_transfers WHERE NOT is_self")
    assert one("SELECT sum(drops) FROM edges") == one("SELECT sum(drops) FROM s_transfers WHERE NOT is_self")
    assert one("SELECT sum(n_payment + n_checkcash + n_escrowfinish) FROM edges") == one("SELECT sum(n_transfers) FROM edges")
    assert one("SELECT sum(n_partial) FROM edges") == \
        one("SELECT count(*) FROM s_transfers WHERE NOT is_self AND partial_payment")
    assert one("SELECT count(*) FROM (SELECT DISTINCT month, from_account, to_account, asset FROM edges)") == \
        one("SELECT count(*) FROM edges")
