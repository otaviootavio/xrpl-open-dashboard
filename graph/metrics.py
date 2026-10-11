"""E4: metrics on the entity graph for one month, every analysed asset; writes graph_nodes, graph_communities,
graph_cycles and graph_summary (one part per month each).
"""

import logging
import time

import random

import igraph as ig
import numpy as np
import pyarrow as pa
from scipy.sparse import csr_matrix

import hodge
import tails
from common import (ALL, GRAPH_VERSION, RLUSD, SEED, Paths, Timer, asset_label, assets_for_month, check_rss,
                    month_bounds, peak_rss_mb, prev_month, q, round_sig, silver_month_files, write_table)
from load import MonthGraph, load_month

log = logging.getLogger("graph")

COMMUNITY_ASSETS = ("XRP", RLUSD, ALL)
VALUE_TAIL_ASSETS = ("XRP", RLUSD)
COMMUNITY_MIN_N, COMMUNITY_TABLE_MIN, LEIDEN_ITERS = 3, 5, 5
CYCLE_MAX, CYCLE_QUANTILE, CYCLE_MIN_N_ALL, CYCLE_HASHED = 10_000, 0.99, 10, 100
HUB_TOP, FLOW_N = 50, 20


def renumber(membership: np.ndarray) -> np.ndarray:
    """Component/community ids by size, largest first; ties by smallest vertex id."""
    membership = np.asarray(membership)
    ids, first, sizes = np.unique(membership, return_index=True, return_counts=True)
    order = np.lexsort((first, -sizes))
    new = np.empty(len(ids), dtype=np.int64)
    new[order] = np.arange(len(ids))
    return new[np.searchsorted(ids, membership)]


def bowtie(g: ig.Graph) -> np.ndarray:
    scc = renumber(g.connected_components(mode="strong").membership)
    part = np.full(g.vcount(), "other", dtype=object)
    if g.vcount() == 0:
        return part
    core = np.flatnonzero(scc == 0)
    v0 = int(core[0])
    part[np.asarray(g.subcomponent(v0, mode="in"))] = "in"
    part[np.asarray(g.subcomponent(v0, mode="out"))] = "out"
    part[core] = "core"
    return part


def m_n(values: np.ndarray, n: int = 20) -> float:
    """Modified inverse HH index M_n of the normalized list (arXiv 2106.10012, eq. 5-6), computed stably."""
    v = values[values > 0]
    if not len(v):
        return 0.0
    mx = v.max()
    u = v / mx
    return float(v.sum() * np.sum(u ** (n - 1)) / (mx * np.sum(u ** n)))


def flow_index(con, p: Paths, month: str, assets: list[str]) -> dict:
    """{asset: {entity: (A1, A2)}} from daily inflows and outflows (arXiv 2106.10012, eq. 13):
    A1 = M20(in) * max(in) / max(in, out), A2 = M20(out) * max(out) / max(in, out); no flow on a side -> 0."""
    d0, d1 = month_bounds(month)
    files = ",".join(q(f) for f in silver_month_files(p, "transfers", month))
    alist = [a for a in assets if a != ALL]
    ge = q(p.part("graph_entities", month))
    con.execute(f"""CREATE OR REPLACE TEMP TABLE fi_daily AS
        WITH t AS (SELECT close_time::DATE AS d, asset, from_account, to_account, value_f
                   FROM read_parquet([{files}], union_by_name = true)
                   WHERE NOT is_self AND close_time >= DATE '{d0}' AND close_time < DATE '{d1}'
                     AND asset IN ({",".join(f"'{a}'" for a in alist)}) AND value_f > 0),
        e AS (SELECT t.d, t.asset, ef.entity_id AS fe, et.entity_id AS te, t.value_f FROM t
              JOIN read_parquet({ge}) ef ON ef.account = t.from_account
              JOIN read_parquet({ge}) et ON et.account = t.to_account
              WHERE ef.entity_id <> et.entity_id),
        f AS (SELECT asset, fe AS entity, 1 AS dir, d, value_f FROM e
              UNION ALL SELECT asset, te, 0, d, value_f FROM e)
        -- plain sum: the ordered sum ran out of memory under the 4 GB cap on 2026-01 (millions of entity-day
        -- groups); the order only moves the last bits, and the Flow Index is rounded to 12 digits when written
        SELECT asset, entity, dir, d, sum(value_f) AS v FROM f GROUP BY ALL""")
    con.execute("""CREATE OR REPLACE TEMP TABLE fi_ent AS
        SELECT entity, (row_number() OVER (ORDER BY entity) - 1)::INTEGER AS eid FROM (SELECT DISTINCT entity FROM fi_daily)""")
    names = [r[0] for r in con.execute("SELECT entity FROM fi_ent ORDER BY eid").fetchall()]
    arr = con.execute("""SELECT list_position(?, f.asset)::INTEGER - 1 AS aid, e.eid, f.dir, f.v
        FROM fi_daily f JOIN fi_ent e USING (entity) ORDER BY aid, eid, dir, f.d""", [alist]).fetchnumpy()
    con.execute("DROP TABLE fi_daily; DROP TABLE fi_ent")
    aid, eid, dr, v = (np.asarray(arr[k]) for k in ("aid", "eid", "dir", "v"))
    if not len(v):
        return {}
    key = (aid.astype(np.int64) * len(names) + eid) * 2 + dr
    starts = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
    gk = key[starts]
    total = np.add.reduceat(v, starts)
    mx = np.maximum.reduceat(v, starts)
    u = v / np.repeat(mx, np.diff(np.r_[starts, len(v)]))
    m20 = total * np.add.reduceat(u ** 19, starts) / (mx * np.add.reduceat(u ** 20, starts))
    side = {}  # (aid, eid) -> [M_in, max_in, M_out, max_out]
    for k, m, x in zip(gk.tolist(), m20.tolist(), mx.tolist()):
        a_e, d = divmod(k, 2)
        rec = side.setdefault(divmod(a_e, len(names)), [0.0, 0.0, 0.0, 0.0])
        rec[2 * d], rec[2 * d + 1] = m, x
    out: dict = {}
    for (a, e), (m_in, x_in, m_out, x_out) in side.items():
        big = max(x_in, x_out)
        out.setdefault(alist[a], {})[names[e]] = (m_in * x_in / big, m_out * x_out / big)
    return out


def communities(mg: MonthGraph) -> tuple[np.ndarray, float]:
    keep = mg.n >= COMMUNITY_MIN_N
    g = ig.Graph(n=len(mg.names), edges=np.column_stack([mg.src[keep], mg.dst[keep]]).tolist(), directed=True)
    g.es["n"] = mg.n[keep].astype(float).tolist()
    u = g.as_undirected(mode="collapse", combine_edges={"n": "sum"})
    # igraph's own Leiden: 11 s and modularity 0.63 on 2025-11 XRP, where leidenalg took 108 s for 0.52
    ig.set_random_number_generator(random.Random(SEED))
    part = u.community_leiden(objective_function="modularity", weights="n", n_iterations=LEIDEN_ITERS)
    return renumber(part.membership), float(u.modularity(part.membership, weights="n"))


def cycles(mg: MonthGraph, w: np.ndarray) -> list[tuple]:
    """2- and 3-cycles: (length, entity ids smallest first, strength), strongest first, at most CYCLE_MAX."""
    nv = len(mg.names)
    a = csr_matrix((w, (mg.src, mg.dst)), shape=(nv, nv))
    mn = a.minimum(a.T).tocoo()
    found = [(2, (int(i), int(j)), float(s)) for i, j, s in zip(mn.row, mn.col, mn.data) if i < j and s > 0]
    keep = (mg.n >= CYCLE_MIN_N_ALL) if mg.asset == ALL else (w >= np.quantile(w, CYCLE_QUANTILE)) & (w > 0)
    ks, kd, kw = mg.src[keep], mg.dst[keep], w[keep]
    wt = {(int(i), int(j)): float(x) for i, j, x in zip(ks, kd, kw)}
    out_n: dict[int, set] = {}
    in_n: dict[int, set] = {}
    for i, j in wt:
        out_n.setdefault(i, set()).add(j)
        in_n.setdefault(j, set()).add(i)
    tri = set()
    for i, j in wt:
        for k in out_n.get(j, set()) & in_n.get(i, set()):
            if k in (i, j):
                continue
            c = (i, j, k)
            r = c.index(min(c))
            tri.add(c[r:] + c[:r])
    for c in tri:
        s = min(wt[(c[0], c[1])], wt[(c[1], c[2])], wt[(c[2], c[0])])
        found.append((3, c, s))
    found.sort(key=lambda x: (-x[2], x[0], x[1]))
    return found[:CYCLE_MAX]


def sample_hashes(con, p: Paths, month: str, pairs: list[tuple]) -> dict:
    """Up to 3 transaction hashes per (from_entity, to_entity, asset), first by ledger order."""
    if not pairs:
        return {}
    d0, d1 = month_bounds(month)
    rows = sorted(set(pairs))
    con.register("want_arrow", pa.table({"fe": [r[0] for r in rows], "te": [r[1] for r in rows],
                                         "asset": [r[2] for r in rows]}))
    con.execute("CREATE OR REPLACE TEMP TABLE want AS SELECT * FROM want_arrow")
    con.unregister("want_arrow")
    files = ",".join(q(f) for f in silver_month_files(p, "transfers", month))
    ge = q(p.part("graph_entities", month))
    rows = con.execute(f"""
        WITH t AS (SELECT ledger_index, hash, coalesce(asset, 'MPT') AS asset, from_account, to_account
                   FROM read_parquet([{files}], union_by_name = true)
                   WHERE NOT is_self AND close_time >= DATE '{d0}' AND close_time < DATE '{d1}'),
        e AS (SELECT t.*, ef.entity_id AS fe, et.entity_id AS te FROM t
              JOIN read_parquet({ge}) ef ON ef.account = t.from_account
              JOIN read_parquet({ge}) et ON et.account = t.to_account)
        SELECT e.fe, e.te, e.asset, e.hash FROM e SEMI JOIN want w ON w.fe = e.fe AND w.te = e.te
            AND (w.asset = e.asset OR w.asset = '*')
        QUALIFY row_number() OVER (PARTITION BY e.fe, e.te, e.asset ORDER BY e.ledger_index, e.hash) <= 3""").fetchall()
    out: dict = {}
    for fe, te, asset, h in rows:
        out.setdefault((fe, te, asset), []).append(h)
        out.setdefault((fe, te, ALL), []).append(h)
    return {k: sorted(v)[:3] for k, v in out.items()}


def roles(con, p: Paths, month: str) -> dict:
    """Entity -> on-ledger role evidence: issuer, amm_pool, likely_custodial (first match wins later)."""
    _, d1 = month_bounds(month)
    ge = q(p.part("graph_entities", month))
    edges = q(p.part("transfer_edges_monthly", month))
    amm = q(p.silver / "amm_events" / "month=*" / "*.parquet")
    r = {}
    for entity, role in con.execute(f"""
        WITH ent AS (SELECT account, entity_id, likely_custodial FROM read_parquet({ge})),
        iss AS (SELECT DISTINCT split_part(asset, '.', 2) AS account FROM read_parquet({edges})
                WHERE asset NOT IN ('XRP', 'MPT')),
        pools AS (SELECT DISTINCT pool AS account FROM read_parquet({amm}) WHERE close_time < DATE '{d1}')
        SELECT entity_id, 'amm_pool' FROM ent SEMI JOIN pools USING (account)
        UNION SELECT entity_id, 'issuer' FROM ent SEMI JOIN iss USING (account)
        UNION SELECT entity_id, 'likely_custodial' FROM ent WHERE likely_custodial""").fetchall():
        r.setdefault(entity, set()).add(role)
    return r


ROLE_ORDER = ("amm_pool", "issuer", "likely_custodial")


def asset_metrics(con, p: Paths, month: str, asset: str, flow: dict, role_ev: dict) -> tuple[dict, dict, list, list]:
    t0 = time.time()
    mg = load_month(con, p, month, asset, level="entity")
    nv, ne = len(mg.names), len(mg.src)
    w = np.nan_to_num(mg.weight, nan=0.0)
    g = mg.to_igraph()
    in_deg = np.asarray(g.degree(mode="in"))
    out_deg = np.asarray(g.degree(mode="out"))
    in_n = np.bincount(mg.dst, weights=mg.n, minlength=nv).astype(np.int64)
    out_n = np.bincount(mg.src, weights=mg.n, minlength=nv).astype(np.int64)
    has_value = asset != ALL
    in_v = np.bincount(mg.dst, weights=mg.value, minlength=nv) if has_value else None
    out_v = np.bincount(mg.src, weights=mg.value, minlength=nv) if has_value else None
    pr = np.asarray(g.pagerank(directed=True, damping=0.85, weights="w")) if ne else np.zeros(nv)
    wcc = renumber(g.connected_components(mode="weak").membership) if nv else np.zeros(0, np.int64)
    bt = bowtie(g)
    comm, modularity = (communities(mg) if asset in COMMUNITY_ASSETS and ne else (None, None))
    hd = hodge.decompose(nv, mg.src, mg.dst, w)
    check_rss(f"{month} {asset_label(asset)} metrics")

    # roles: on-ledger evidence first, then bot pair, then hub by PageRank
    hub = set(np.lexsort((np.arange(nv), -pr))[:HUB_TOP].tolist())
    top_partner_share = np.zeros(nv)
    if ne:
        order = np.lexsort((-mg.n, mg.src))
        first = np.ones(ne, bool)
        first[1:] = mg.src[order][1:] != mg.src[order][:-1]
        top_partner_share[mg.src[order][first]] = mg.n[order][first] / np.maximum(out_n[mg.src[order][first]], 1)
        pairs = set(zip(mg.src.tolist(), mg.dst.tolist()))
        top_partner = np.full(nv, -1)
        top_partner[mg.src[order][first]] = mg.dst[order][first]
    fl = flow.get(asset, {})
    node_rows = []
    for v in range(nv):
        name = mg.names[v]
        ev = role_ev.get(name, set())
        role = next((r for r in ROLE_ORDER if r in ev), None)
        if role is None and ne and top_partner_share[v] >= 0.5 and (int(top_partner[v]), v) in pairs:
            role = "bot_pair"
        if role is None:
            role = "hub" if v in hub else "unknown"
        a1, a2 = fl.get(name, (None, None))
        node_rows.append((name, int(in_deg[v]), int(out_deg[v]), int(in_n[v]), int(out_n[v]),
                          round_sig(float(in_v[v])) if has_value else None,
                          round_sig(float(out_v[v])) if has_value else None,
                          round_sig(float(pr[v])), int(wcc[v]), bt[v], round_sig(a1), round_sig(a2),
                          int(comm[v]) if comm is not None else None, round_sig(float(hd["potential"][v])), role))

    comm_rows = []
    if comm is not None:
        sizes = np.bincount(comm)
        cs, cd = comm[mg.src], comm[mg.dst]
        inside = cs == cd
        int_n = np.bincount(cs[inside], weights=mg.n[inside], minlength=len(sizes))
        int_v = np.bincount(cs[inside], weights=w[inside], minlength=len(sizes))
        cross = ~inside
        key = cs[cross].astype(np.int64) * len(sizes) + cd[cross]
        uk, inv = np.unique(key, return_inverse=True)
        fn = np.bincount(inv, weights=mg.n[cross])
        fv = np.bincount(inv, weights=w[cross])
        flows: dict[int, list] = {}
        for k, a, b in zip(uk, fn, fv):
            flows.setdefault(int(k // len(sizes)), []).append((int(k % len(sizes)), int(a), round_sig(float(b))))
        by_comm = np.lexsort((np.arange(nv), -pr, comm))
        starts = np.searchsorted(comm[by_comm], np.arange(len(sizes)))
        for c in range(len(sizes)):
            if sizes[c] < COMMUNITY_TABLE_MIN:
                break
            members = by_comm[starts[c]:starts[c] + 5]
            fo = sorted(flows.get(c, []), key=lambda x: (-x[1], x[0]))[:FLOW_N]
            comm_rows.append((c, int(sizes[c]), int(int_n[c]), round_sig(float(int_v[c])) if has_value else None,
                              round_sig(modularity), [str(mg.names[m]) for m in members],
                              [{"to": a, "n": b, "value": c2 if has_value else None} for a, b, c2 in fo]))

    cyc = cycles(mg, w) if ne else []
    cyc_rows = [(k, length, [str(mg.names[x]) for x in ids], round_sig(s)) for k, (length, ids, s) in enumerate(cyc)]

    # churn against the previous month (same asset)
    pm = prev_month(month)
    churn = {"entities_new": None, "entities_gone": None, "edges_new": None, "edges_gone": None,
             "value_share_new_edges": None}
    if p.part("graph_entity_edges", pm).exists():
        aw = "" if asset == ALL else f"WHERE asset = '{asset}'"
        wcol = "sum(n_transfers)::DOUBLE" if asset == ALL else "sum(value_f ORDER BY value_f)"
        row = con.execute(f"""
            WITH c AS (SELECT from_entity f, to_entity t, {wcol} AS w FROM read_parquet({q(p.part('graph_entity_edges', month))}) {aw} GROUP BY 1, 2),
                 o AS (SELECT from_entity f, to_entity t FROM read_parquet({q(p.part('graph_entity_edges', pm))}) {aw} GROUP BY 1, 2),
                 cn AS (SELECT f AS x FROM c UNION SELECT t FROM c), onn AS (SELECT f AS x FROM o UNION SELECT t FROM o)
            SELECT (SELECT count(*) FROM cn ANTI JOIN onn USING (x)), (SELECT count(*) FROM onn ANTI JOIN cn USING (x)),
                   (SELECT count(*) FROM c ANTI JOIN o USING (f, t)), (SELECT count(*) FROM o ANTI JOIN c USING (f, t)),
                   (SELECT sum(w ORDER BY w) FROM c ANTI JOIN o USING (f, t)) / nullif((SELECT sum(w ORDER BY w) FROM c), 0)
            """).fetchone()
        churn = dict(zip(churn, [row[0], row[1], row[2], row[3], round_sig(row[4])]))

    a = csr_matrix((w, (mg.src, mg.dst)), shape=(nv, nv)) if ne else None
    r_w = float(a.minimum(a.T).sum() / a.sum()) if ne and a.sum() > 0 else None
    tin = tails.fit(in_deg[in_deg > 0], True)
    tout = tails.fit(out_deg[out_deg > 0], True)
    tval = tails.fit(mg.value, False) if asset in VALUE_TAIL_ASSETS else {}
    n_out = sum(1 for x in fl.values() if x[0] <= 0.5)
    n_in = sum(1 for x in fl.values() if x[1] <= 0.5)
    summary = {
        "asset": asset, "asset_label": asset_label(asset), "n_nodes": nv, "n_edges": ne,
        "n_transfers": int(mg.n.sum()), "value_total": round_sig(float(np.sum(np.sort(mg.value)))) if has_value else None,
        **{f"tail_in_{k}": round_sig(v) if isinstance(v, float) else v for k, v in tin.items()},
        **{f"tail_out_{k}": round_sig(v) if isinstance(v, float) else v for k, v in tout.items()},
        **{f"tail_value_{k}": round_sig(tval.get(k)) if isinstance(tval.get(k), float) else tval.get(k)
           for k in ("alpha", "xmin", "n_tail", "ks", "R_lognormal", "p_lognormal")},
        "giant_wcc_share": round_sig(float(np.mean(wcc == 0))) if nv else None,
        **{f"bowtie_{k}_share": round_sig(float(np.mean(bt == k))) if nv else None for k in ("core", "in", "out", "other")},
        "reciprocity": round_sig(float(g.reciprocity(ignore_loops=True))) if ne else None,
        "reciprocity_weighted": round_sig(r_w),
        "modularity": round_sig(modularity), "n_communities": int((np.bincount(comm) >= 2).sum()) if comm is not None else None,
        "gradient_share": round_sig(hd["gradient_share"]), "loop_share": round_sig(hd["loop_share"]),
        "curl_share": None, "harmonic_share": None, "lsqr_iters": hd["lsqr_iters"],
        "flow_index_out_class": n_out if fl else None, "flow_index_in_class": n_in if fl else None,
        "n_cycles": len(cyc), **churn, "seconds": round(time.time() - t0, 1), "peak_rss_mb": peak_rss_mb(),
    }
    log.info("%s %s: %d nodes, %d edges, loop %.3f, %.1f s", month, asset_label(asset), nv, ne,
             hd["loop_share"] or 0, time.time() - t0)
    return summary, {"nodes": node_rows}, comm_rows, cyc_rows


NODE_COLS = ["entity_id", "in_deg", "out_deg", "in_n", "out_n", "in_value", "out_value", "pagerank", "wcc",
             "bowtie_part", "flow_in", "flow_out", "community", "hodge_potential", "role"]
NODE_TYPES = [pa.string(), pa.int64(), pa.int64(), pa.int64(), pa.int64(), pa.float64(), pa.float64(), pa.float64(),
              pa.int64(), pa.string(), pa.float64(), pa.float64(), pa.int64(), pa.float64(), pa.string()]


def build(con, p: Paths, month: str, assets: list[str] | None = None) -> dict:
    assets = assets or assets_for_month(con, p, month)
    md = month_bounds(month)[0]
    with Timer(f"{month} metrics: flow index"):
        flow = flow_index(con, p, month, assets)
    role_ev = roles(con, p, month)
    nodes, comms, cycs, summ = [], [], [], []
    hash_pairs = []
    for asset in assets:
        s, nd, cr, cy = asset_metrics(con, p, month, asset, flow, role_ev)
        summ.append(s)
        nodes += [(asset, *r) for r in nd["nodes"]]
        comms += [(asset, *r) for r in cr]
        cycs += [(asset, *r) for r in cy]
        for _, _, ents, _ in cy[:CYCLE_HASHED]:
            hash_pairs += [(ents[i], ents[(i + 1) % len(ents)], asset) for i in range(len(ents))]
    with Timer(f"{month} metrics: cycle hashes"):
        hashes = sample_hashes(con, p, month, hash_pairs)

    cols = list(zip(*nodes)) if nodes else [[] for _ in range(len(NODE_COLS) + 1)]
    t = pa.table({"month": pa.array([md] * len(nodes), pa.date32()), "asset": pa.array(cols[0], pa.string()),
                  **{c: pa.array(cols[i + 1], ty) for i, (c, ty) in enumerate(zip(NODE_COLS, NODE_TYPES))},
                  "graph_version": [GRAPH_VERSION] * len(nodes)})
    n_nodes = write_table(t, p.part("graph_nodes", month), ["asset", "entity_id"])

    t = pa.table({"month": pa.array([md] * len(comms), pa.date32()),
                  "asset": pa.array([r[0] for r in comms], pa.string()),
                  "community": pa.array([r[1] for r in comms], pa.int64()),
                  "size": pa.array([r[2] for r in comms], pa.int64()),
                  "internal_n": pa.array([r[3] for r in comms], pa.int64()),
                  "internal_value": pa.array([r[4] for r in comms], pa.float64()),
                  "modularity": pa.array([r[5] for r in comms], pa.float64()),
                  "top_entities": pa.array([r[6] for r in comms], pa.list_(pa.string())),
                  "flows_out": pa.array([r[7] for r in comms], pa.list_(pa.struct(
                      [("to", pa.int64()), ("n", pa.int64()), ("value", pa.float64())]))),
                  "graph_version": [GRAPH_VERSION] * len(comms)})
    write_table(t, p.part("graph_communities", month), ["asset", "community"])

    def cyc_hashes(asset, ents):
        out = []
        for i in range(len(ents)):
            out += hashes.get((ents[i], ents[(i + 1) % len(ents)], asset), [])
        return out

    t = pa.table({"month": pa.array([md] * len(cycs), pa.date32()),
                  "asset": pa.array([r[0] for r in cycs], pa.string()),
                  "cycle_id": pa.array([r[1] for r in cycs], pa.int64()),
                  "length": pa.array([r[2] for r in cycs], pa.int64()),
                  "entities": pa.array([r[3] for r in cycs], pa.list_(pa.string())),
                  "strength": pa.array([r[4] for r in cycs], pa.float64()),
                  "sample_hashes": pa.array([cyc_hashes(r[0], r[3]) if r[1] < CYCLE_HASHED else [] for r in cycs],
                                            pa.list_(pa.string())),
                  "graph_version": [GRAPH_VERSION] * len(cycs)})
    write_table(t, p.part("graph_cycles", month), ["asset", "cycle_id"])

    keys = list(summ[0])
    t = pa.table({"month": pa.array([md] * len(summ), pa.date32()),
                  **{k: [s[k] for s in summ] for k in keys}, "assets_analysed": [len(assets)] * len(summ),
                  "graph_version": [GRAPH_VERSION] * len(summ)})
    # timings vary run to run: keep them in the log and graph_parts, not in the deterministic table
    t = t.drop_columns(["seconds", "peak_rss_mb"])
    write_table(t, p.part("graph_summary", month), ["asset"])
    return {"assets": len(assets), "node_rows": n_nodes, "communities": len(comms), "cycles": len(cycs)}
