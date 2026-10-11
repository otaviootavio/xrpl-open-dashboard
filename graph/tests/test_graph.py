"""Graph stage tests on small hand-built graphs with known answers.

    uv run pytest graph/tests -q
"""

import random
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import common  # noqa: E402
import entities  # noqa: E402
import hodge  # noqa: E402
import metrics  # noqa: E402
import tails  # noqa: E402
from load import MonthGraph  # noqa: E402


def mgraph(edges, n=None, asset="XRP"):
    src = np.array([e[0] for e in edges], np.int32)
    dst = np.array([e[1] for e in edges], np.int32)
    w = np.array([e[2] if len(e) > 2 else 1.0 for e in edges])
    nv = n or int(max(src.max(), dst.max()) + 1)
    return MonthGraph("2026-01", asset, np.array([f"r{i:03d}" for i in range(nv)], object), src, dst,
                      np.array([int(e[3]) if len(e) > 3 else 5 for e in edges], np.int64), w)


# ---------------------------------------------------------------- metrics

def test_renumber_by_size_then_first_vertex():
    assert metrics.renumber(np.array([7, 7, 3, 3, 3, 9])).tolist() == [1, 1, 0, 0, 0, 2]


def test_pagerank_matches_power_iteration():
    edges = [(0, 1, 2.0), (1, 2, 1.0), (2, 0, 1.0), (2, 3, 3.0), (3, 4, 1.0), (4, 0, 1.0)]
    mg = mgraph(edges)
    pr = np.asarray(mg.to_igraph().pagerank(directed=True, damping=0.85, weights="w"))
    n = 5
    p = np.zeros((n, n))
    for s, d, w in edges:
        p[d, s] += w
    p /= p.sum(axis=0)
    x = np.full(n, 1 / n)
    for _ in range(500):
        x = 0.15 / n + 0.85 * p @ x
    assert np.allclose(pr, x, atol=1e-9)


def test_bowtie_parts():
    # 0 -> {1,2,3 cycle} -> 4 ; 5 isolated-ish tendril from 0
    mg = mgraph([(0, 1), (1, 2), (2, 3), (3, 1), (3, 4), (0, 5)])
    part = metrics.bowtie(mg.to_igraph()).tolist()
    assert part == ["in", "core", "core", "core", "out", "other"]


def test_m_n_matches_paper_test_family():
    m, r, n = 3, 0.4, 20
    assert metrics.m_n(np.array([1, 1, 1, r]) * 7.5) == pytest.approx((m + r) * (m + r ** (n - 1)) / (m + r ** n))
    assert metrics.m_n(np.array([5.0, 0, 0])) == pytest.approx(1.0)
    assert metrics.m_n(np.array([])) == 0.0


def test_two_cliques_give_two_communities_every_time():
    edges = [(a, b, 1.0, 5) for base in (0, 10) for a in range(base, base + 10) for b in range(base, base + 10) if a != b]
    edges.append((0, 10, 1.0, 5))
    mg = mgraph(edges)
    first, q = metrics.communities(mg)
    again, _ = metrics.communities(mg)
    assert len(set(first.tolist())) == 2 and first.tolist() == again.tolist() and q > 0.4
    assert len(set(first[:10])) == 1 and len(set(first[10:])) == 1


def test_cycles_found_whatever_the_order():
    edges = [(0, 1, 10.0), (1, 2, 10.0), (2, 0, 10.0),      # 3-cycle
             (3, 4, 9.0), (4, 5, 9.0), (5, 3, 9.0),         # 3-cycle
             (6, 7, 4.0), (7, 6, 2.0)]                      # 2-cycle, strength 2
    edges += [(8 + i, 9 + i, 0.001) for i in range(1000)]   # light chain: the 6 cycle edges are the top 1%
    out = metrics.cycles(mgraph(edges), np.array([e[2] for e in edges]))
    shuffled = edges[:]
    random.Random(1).shuffle(shuffled)
    out2 = metrics.cycles(mgraph(shuffled), np.array([e[2] for e in shuffled]))
    assert out == out2
    assert (3, (0, 1, 2), 10.0) in out and (3, (3, 4, 5), 9.0) in out and (2, (6, 7), 2.0) in out


# ---------------------------------------------------------------- hodge

def test_hodge_pure_cycle_is_all_loop():
    r = hodge.decompose(3, np.array([0, 1, 2]), np.array([1, 2, 0]), np.array([5.0, 5.0, 5.0]))
    assert r["loop_share"] == pytest.approx(1.0) and r["gradient_share"] == pytest.approx(0.0, abs=1e-12)


def test_hodge_potential_flow_is_all_gradient():
    rng = np.random.default_rng(1)
    n = 50
    s = rng.normal(size=n)
    pairs = sorted({(min(a, b), max(a, b)) for a, b in zip(rng.integers(0, n, 400), rng.integers(0, n, 400)) if a != b})
    src, dst = np.array([a for a, _ in pairs]), np.array([b for _, b in pairs])
    r = hodge.decompose(n, src, dst, s[dst] - s[src])
    assert r["gradient_share"] == pytest.approx(1.0, abs=1e-9)
    assert np.allclose(r["potential"], s - s.mean(), atol=1e-6)  # flow runs from low to high potential


def test_hodge_shares_add_up():
    rng = np.random.default_rng(2)
    src, dst = rng.integers(0, 40, 300), rng.integers(0, 40, 300)
    keep = src != dst
    r = hodge.decompose(40, src[keep], dst[keep], rng.random(keep.sum()))
    assert r["gradient_share"] + r["loop_share"] == pytest.approx(1.0, abs=1e-9)


# ---------------------------------------------------------------- tails

def test_tail_fit_recovers_exponent():
    rng = np.random.default_rng(0)
    x = (rng.pareto(1.5, 200_000) + 1) * 10           # density exponent 2.5 above xmin 10
    fit = tails.fit(x, discrete=False)
    assert fit["alpha"] == pytest.approx(2.5, abs=0.05)


# ---------------------------------------------------------------- entities

def write(path: Path, table: pa.Table):
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)


@pytest.fixture()
def tiny(tmp_path, monkeypatch):
    """A month with an exchange-like funder, a bot farm, an issuer with a distribution wallet, and noise."""
    monkeypatch.setattr(entities, "HUB_TOP", 3)  # in a graph this small the farm itself ranks in a top 50
    edges, life = [], []

    def e(f, t, asset="XRP", n=1, v=1.0, tagged=0):
        edges.append((f, t, asset, n, v, tagged))

    for k in range(1500):                       # exchange: funds 1,500 customers who pay outsiders
        c = f"rCust{k:04d}"
        life.append((c, "rExchange"))
        e("rExchange", c, n=1)
        e(c, f"rShop{k % 7}", n=4)
    for k in range(20):                         # bot farm: 20 children that pay each other and the funder
        c = f"rBot{k:02d}"
        life.append((c, "rFarm"))
        e(c, f"rBot{(k + 1) % 20:02d}", n=8)
        e(c, "rFarm", n=2)
    life.append(("rBot01", "rFarm"))            # deleted and created again: a second 'created' row
    tok = "5553440000000000000000000000000000000000.rIssuer"
    e("rIssuer", "rHot", asset=tok, v=900.0)    # 90% of the issuer's outflow
    e("rIssuer", "rElse", asset=tok, v=100.0)
    for k in range(50):
        e("rHot", f"rHolder{k:02d}", asset=tok, v=10.0)
    for k in range(30):                         # tagged deposits
        e(f"rUser{k:02d}", "rDeposit", n=1, tagged=1)
    month = date(2026, 1, 1)
    write(tmp_path / "gold/transfer_edges_monthly/month=2026-01.parquet", pa.table({
        "month": [month] * len(edges), "from_account": [x[0] for x in edges], "to_account": [x[1] for x in edges],
        "asset": [x[2] for x in edges], "currency_code": [None] * len(edges),
        "n_transfers": pa.array([x[3] for x in edges], pa.int64()), "drops": pa.array([None] * len(edges), pa.int64()),
        "value_f": [x[4] for x in edges], "n_partial": pa.array([0] * len(edges), pa.int64()),
        "n_tagged": pa.array([x[5] * x[3] for x in edges], pa.int64()),
        "n_distinct_tags": pa.array([x[5] for x in edges], pa.int64())}))
    from datetime import datetime, timezone
    write(tmp_path / "silver/account_lifecycle/month=2025-12/x.parquet", pa.table({
        "close_time": [datetime(2025, 12, 15, tzinfo=timezone.utc)] * len(life), "event": ["created"] * len(life),
        "account": [x[0] for x in life], "funded_by": [x[1] for x in life], "is_amm": [False] * len(life)}))
    write(tmp_path / "gold/dest_tag_monthly/month=2026-01.parquet", pa.table({
        "month": [month], "account": ["rDeposit"], "distinct_tags": pa.array([150], pa.int64()),
        "transfers": pa.array([30], pa.int64())}))
    p = common.Paths(tmp_path)
    return p, common.connect(p)


def entity_map(p):
    t = pq.read_table(p.part("graph_entities", "2026-01")).to_pydict()
    return dict(zip(t["account"], t["entity_id"])), dict(zip(t["account"], t["rule"])), \
        dict(zip(t["account"], t["likely_custodial"]))


def test_entity_rules(tiny):
    p, con = tiny
    out = entities.build(con, p, "2026-01")
    ent, rule, cust = entity_map(p)
    assert all(ent[f"rCust{k:04d}"] == f"rCust{k:04d}" for k in range(1500))     # exchange customers stay apart
    assert len({ent[f"rBot{k:02d}"] for k in range(20)} | {ent["rFarm"]}) == 1     # the farm is one entity
    assert rule["rBot00"] == "funding" and ent["rFarm"] == min(["rFarm", *[f"rBot{k:02d}" for k in range(20)]])
    assert ent["rHot"] == ent["rIssuer"] and rule["rHot"] == "issuer"
    assert ent["rElse"] == "rElse"
    assert cust["rDeposit"] and not cust["rExchange"]
    assert out["internal_transfers"] > 0
    t = pq.read_table(p.part("graph_entities", "2026-01")).to_pydict()
    import json
    shares = [e["in_tree_share"] for ev in t["evidence"] if ev for e in json.loads(ev) if e["rule"] == "funding"]
    assert shares and max(shares) <= 1.0  # a re-created account must not count its transfers twice


def test_entity_edges_conserve_transfers(tiny):
    p, con = tiny
    entities.build(con, p, "2026-01")
    acc = pq.read_table(p.part("transfer_edges_monthly", "2026-01")).column("n_transfers").to_pylist()
    ent = pq.read_table(p.part("graph_entity_edges", "2026-01")).to_pydict()
    emap, _, _ = entity_map(p)
    assert all(f != t for f, t in zip(ent["from_entity"], ent["to_entity"]))
    assert sum(ent["n_transfers"]) < sum(acc)  # internal farm/issuer transfers dropped (build() asserts the exact sum)


def test_entities_deterministic(tiny):
    p, con = tiny
    entities.build(con, p, "2026-01")
    a = con.execute(f"SELECT sum(hash(x)) FROM read_parquet({common.q(p.part('graph_entity_edges', '2026-01'))}) x").fetchone()
    entities.build(con, p, "2026-01")
    b = con.execute(f"SELECT sum(hash(x)) FROM read_parquet({common.q(p.part('graph_entity_edges', '2026-01'))}) x").fetchone()
    assert a == b


def test_union_find_size_cap(monkeypatch, tiny):
    p, con = tiny
    monkeypatch.setattr(entities, "GROUP_MAX", 10)  # the 21-account farm is now too big
    out = entities.build(con, p, "2026-01")
    ent, _, _ = entity_map(p)
    assert ent["rBot00"] == "rBot00" and out["rejected_groups"] == 1
