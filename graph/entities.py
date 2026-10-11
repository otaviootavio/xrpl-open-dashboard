"""E3: group accounts into entities with on-ledger evidence, then sum account edges into entity edges.

Rule 1 (issuer): the account that receives >= 50% of an issuer's outflow of a token and sends that token
to >= 10 receivers joins the issuer. Rule 2 (funding): a child joins its funder when the funder created at
most 1,000 accounts up to the end of the month and >= 80% of the child's transfers (>= 5) are with the
funder or its other children. Same funder alone never means same owner: exchanges activate tens of
thousands of customer accounts. Groups are connected components of the accepted links; a group over
10,000 accounts, or one that would join two top-50 hubs, is logged and not applied.
"""

import logging

import numpy as np
import pyarrow as pa

from common import ALL, GRAPH_VERSION, Paths, Timer, check_rss, copy_atomic, dumps, month_bounds, q, round_sig, write_table
from load import load_month

log = logging.getLogger("graph")

ISSUER_SHARE, ISSUER_RECEIVERS = 0.5, 10
FUNDER_MAX_CHILDREN, IN_TREE_SHARE, CHILD_MIN_TRANSFERS = 1000, 0.8, 5
GROUP_MAX, HUB_TOP = 10_000, 50
CUSTODIAL_MIN_IN, CUSTODIAL_SHARE = 20, 0.9


def issuer_links(con, edges: str) -> list[tuple]:
    return con.execute(f"""
        WITH e AS (SELECT * FROM read_parquet({edges}) WHERE asset NOT IN ('XRP', 'MPT')),
        out AS (SELECT asset, from_account AS issuer, to_account AS h, value_f FROM e
                WHERE from_account = split_part(asset, '.', 2)),
        tot AS (SELECT asset, sum(value_f ORDER BY value_f) AS tot FROM out GROUP BY asset),
        recv AS (SELECT asset, from_account AS h, count(DISTINCT to_account) AS nr FROM e GROUP BY ALL)
        SELECT o.issuer, o.h, o.asset, o.value_f / t.tot AS share, r.nr
        FROM out o JOIN tot t USING (asset) JOIN recv r ON r.asset = o.asset AND r.h = o.h
        WHERE t.tot > 0 AND o.value_f >= {ISSUER_SHARE} * t.tot AND r.nr >= {ISSUER_RECEIVERS} AND o.h <> o.issuer
        ORDER BY 1, 2, 3""").fetchall()


def funding_links(con, p: Paths, edges: str, month_end) -> list[tuple]:
    lc = q(p.silver / "account_lifecycle" / "month=*" / "*.parquet")
    return con.execute(f"""
        -- an account deleted and created again has two 'created' rows: keep the latest creation
        WITH lc AS (SELECT account, funded_by FROM read_parquet({lc})
                    WHERE event = 'created' AND close_time < DATE '{month_end}' AND NOT is_amm
                    QUALIFY row_number() OVER (PARTITION BY account ORDER BY close_time DESC, funded_by) = 1),
        size AS (SELECT funded_by, count(*) AS children FROM lc GROUP BY 1),
        fam AS (SELECT lc.account, lc.funded_by AS f, s.children FROM lc JOIN size s USING (funded_by)
                WHERE s.children <= {FUNDER_MAX_CHILDREN}),
        e AS (SELECT from_account AS a, to_account AS b, sum(n_transfers) AS n FROM read_parquet({edges}) GROUP BY 1, 2),
        ce AS (SELECT a, b, n FROM e UNION ALL SELECT b, a, n FROM e),
        tot AS (SELECT a, sum(n) AS tot FROM ce WHERE a IN (SELECT account FROM fam) GROUP BY a),
        inn AS (SELECT ce.a, sum(ce.n) AS inn FROM ce JOIN fam ca ON ca.account = ce.a
                LEFT JOIN fam cb ON cb.account = ce.b
                WHERE ce.b = ca.f OR cb.f = ca.f GROUP BY ce.a)
        SELECT fam.f, fam.account, fam.children, inn.inn::DOUBLE / tot.tot AS share, tot.tot
        FROM fam JOIN tot ON tot.a = fam.account JOIN inn ON inn.a = fam.account
        WHERE tot.tot >= {CHILD_MIN_TRANSFERS} AND inn.inn >= {IN_TREE_SHARE} * tot.tot
        ORDER BY 1, 2""").fetchall()


class UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        root = x
        while self.parent.get(root, root) != root:
            root = self.parent[root]
        while x != root:
            self.parent[x], x = root, self.parent.get(x, x)
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            lo, hi = sorted((ra, rb))
            self.parent[hi] = lo
            self.parent.setdefault(lo, lo)


def top_hubs(con, p: Paths, month: str) -> set[str]:
    mg = load_month(con, p, month, ALL, level="account")
    pr = np.asarray(mg.to_igraph().pagerank(directed=True, damping=0.85, weights="w"))
    order = np.lexsort((np.arange(len(pr)), -pr))[:HUB_TOP]
    return set(mg.names[order].tolist())


def build(con, p: Paths, month: str) -> dict:
    """Write graph_entities and graph_entity_edges for one month; return counts for the log."""
    edges = q(p.part("transfer_edges_monthly", month))
    _, month_end = month_bounds(month)
    with Timer(f"{month} entities: rules"):
        iss = issuer_links(con, edges)
        fund = funding_links(con, p, edges, month_end)
        hubs = top_hubs(con, p, month)
    links = sorted([("issuer", a, b, {"token": t, "share": round_sig(s), "receivers": r}) for a, b, t, s, r in iss]
                   + [("funding", a, b, {"funder": a, "tree": c, "in_tree_share": round_sig(s), "transfers": n})
                      for a, b, c, s, n in fund], key=lambda x: (x[0], x[1], x[2]))
    uf = UnionFind()
    for _, a, b, _ in links:
        uf.union(a, b)
    members: dict[str, list[str]] = {}
    for x in list(uf.parent):
        members.setdefault(uf.find(x), []).append(x)
    rejected = {}
    for root, ms in members.items():
        if len(ms) > GROUP_MAX:
            rejected[root] = f"size {len(ms)} > {GROUP_MAX}"
        elif len(hubs.intersection(ms)) >= 2:
            rejected[root] = "joins top-50 hubs " + ", ".join(sorted(hubs.intersection(ms)))
    for root, why in sorted(rejected.items()):
        log.warning("%s entities: group of %s not applied: %s", month, root, why)

    entity, rule, evidence = {}, {}, {}
    group_rules: dict[str, str] = {}
    for kind, a, b, ev in links:
        root = uf.find(a)
        if root in rejected:
            continue
        group_rules[root] = "issuer" if kind == "issuer" or group_rules.get(root) == "issuer" else "funding"
        for x in (a, b):
            evidence.setdefault(x, []).append({"rule": kind, "from": a, "to": b, **ev})
    for root, ms in members.items():
        if root in rejected:
            continue
        for x in ms:
            entity[x], rule[x] = root, group_rules[root]

    accounts = [r[0] for r in con.execute(f"""SELECT a FROM (SELECT from_account AS a FROM read_parquet({edges})
        UNION SELECT to_account FROM read_parquet({edges})) ORDER BY a""").fetchall()]
    cust = dict(con.execute(f"""SELECT to_account, sum(n_tagged)::DOUBLE / sum(n_transfers) FROM read_parquet({edges})
        GROUP BY 1 HAVING sum(n_transfers) >= {CUSTODIAL_MIN_IN}""").fetchall())
    dt = p.part("dest_tag_monthly", month)
    tagged = {r[0] for r in con.execute(f"SELECT account FROM read_parquet({q(dt)})").fetchall()} if dt.exists() else set()
    ent = pa.table({
        "month": pa.array([month_bounds(month)[0]] * len(accounts), pa.date32()),
        "account": accounts,
        "entity_id": [entity.get(a, a) for a in accounts],
        "rule": [rule.get(a, "singleton") for a in accounts],
        "evidence": [dumps(sorted(evidence[a], key=dumps)) if a in evidence else None for a in accounts],
        "custodial_score": pa.array([round_sig(cust.get(a)) for a in accounts], pa.float64()),
        "likely_custodial": [bool(cust.get(a) is not None and cust[a] >= CUSTODIAL_SHARE and a in tagged) for a in accounts],
        "graph_version": [GRAPH_VERSION] * len(accounts),
    })
    n_ent = write_table(ent, p.part("graph_entities", month), ["account"])
    check_rss(f"{month} entities")

    with Timer(f"{month} entities: entity edges"):
        ge = q(p.part("graph_entities", month))
        sql = f"""
            WITH e AS (SELECT x.month, ef.entity_id AS from_entity, et.entity_id AS to_entity, x.*
                       EXCLUDE (month) FROM read_parquet({edges}) x
                       JOIN read_parquet({ge}) ef ON ef.account = x.from_account
                       JOIN read_parquet({ge}) et ON et.account = x.to_account),
            v AS (SELECT month, from_entity, to_entity, asset, sum(value_f ORDER BY value_f) AS value_f
                  FROM e WHERE from_entity <> to_entity GROUP BY ALL),
            c AS (SELECT month, from_entity, to_entity, asset, sum(n_transfers)::BIGINT AS n_transfers,
                         sum(drops)::BIGINT AS drops, sum(n_partial)::BIGINT AS n_partial,
                         count(DISTINCT from_account)::BIGINT AS n_accounts_from,
                         count(DISTINCT to_account)::BIGINT AS n_accounts_to
                  FROM e WHERE from_entity <> to_entity GROUP BY ALL)
            SELECT c.month, c.from_entity, c.to_entity, c.asset, c.n_transfers, c.drops, v.value_f, c.n_partial,
                   c.n_accounts_from, c.n_accounts_to, '{GRAPH_VERSION}' AS graph_version
            FROM c JOIN v USING (month, from_entity, to_entity, asset)
            ORDER BY from_entity, to_entity, asset"""
        n_edges = copy_atomic(con, sql, p.part("graph_entity_edges", month))
        internal, total, kept = con.execute(f"""SELECT
            (SELECT coalesce(sum(n_transfers), 0) FROM read_parquet({edges}) x
               JOIN read_parquet({ge}) ef ON ef.account = x.from_account
               JOIN read_parquet({ge}) et ON et.account = x.to_account WHERE ef.entity_id = et.entity_id),
            (SELECT sum(n_transfers) FROM read_parquet({edges})),
            (SELECT sum(n_transfers) FROM read_parquet({q(p.part('graph_entity_edges', month))}))""").fetchone()
        if kept + internal != total:
            raise AssertionError(f"{month}: entity edges {kept} + internal {internal} != account edges {total}")
    grouped = sum(1 for a in accounts if a in entity)
    out = {"accounts": len(accounts), "entities": len(set(entity.get(a, a) for a in accounts)), "grouped": grouped,
           "issuer_links": len(iss), "funding_links": len(fund), "rejected_groups": len(rejected),
           "entity_rows": n_ent, "entity_edges": n_edges, "internal_transfers": internal,
           "likely_custodial": int(sum(ent.column("likely_custodial").to_pylist()))}
    log.info("%s entities: %s", month, out)
    return out
