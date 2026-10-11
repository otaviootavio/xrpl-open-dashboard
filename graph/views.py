"""E5: precompute the JSON views drawn by report/graph.html (graphology serialized format, positions set).

Per month and view asset (XRP, RLUSD, all): a backbone (top entities by weighted degree), a community map
(communities named by graph/names.py), one bundle with the largest communities' own entity views, and ego
views for the top entities (personalized PageRank). Plus loops.json (Hodge loop share over time and the
strongest cycles) and index.json (what exists). The browser never runs a layout: graph/layout.py places every
node without overlaps (E7) and graph/backbone.py marks the edges drawn by default (E8).
"""

import heapq
import json
import logging
import math

import numpy as np

import backbone
import layout
from common import ALL, GRAPH_VERSION, RLUSD, VIEWS_VERSION, Paths, Timer, asset_label, prev_month, q
from load import load_month
from metrics import sample_hashes
from names import community_names, community_summary

log = logging.getLogger("graph")

VIEW_ASSETS = ("XRP", RLUSD, ALL)
BACKBONE_NODES, MAX_EDGES, COMMUNITY_MIN_SIZE, COMMUNITY_EDGES = 300, 20_000, 20, 2_000
EGO_TOP, EGO_NEIGHBOURS = 100, 50
BACKBONE_EDGES, EGO_EDGES = 1_000, 200                  # E8: edges drawn by default (bb = 1)
DETAIL_TOP, DETAIL_NODES, DETAIL_EDGES = 10, 150, 400   # E10: community bundles


def sig(x, d: int = 6):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    if isinstance(x, (int, np.integer)):
        return int(x)
    return float(f"{float(x):.{d}g}")


def start_positions(names: list[str], prev: dict) -> list[tuple[float, float]]:
    """Previous month's position when known, else a circle ordered by name."""
    out, k = [], 0
    for i, nm in enumerate(names):
        if nm in prev:
            out.append(tuple(prev[nm]))
        else:
            a = 2 * math.pi * k / max(len(names), 1)
            out.append((math.cos(a), math.sin(a)))
            k += 1
    return out


def load_prev(path) -> dict:
    try:
        d = json.loads(path.read_text())
        return {n["key"]: (n["attributes"]["x"], n["attributes"]["y"]) for n in d["nodes"]}
    except (OSError, ValueError, KeyError):
        return {}


def node_table(con, p: Paths, month: str, asset: str) -> dict:
    cur = con.execute(f"""SELECT n.*, e.n_accounts FROM read_parquet({q(p.part('graph_nodes', month))}) n
        LEFT JOIN (SELECT entity_id, count(*) AS n_accounts FROM read_parquet({q(p.part('graph_entities', month))})
                   GROUP BY 1) e USING (entity_id)
        WHERE n.asset = ?""", [asset])
    cols = [d[0] for d in cur.description]
    return {r[cols.index("entity_id")]: dict(zip(cols, r)) for r in cur.fetchall()}


def node_attrs(nd: dict, x: float, y: float, size: float) -> dict:
    role = nd.get("role") or "unknown"
    return {"x": sig(x), "y": sig(y), "size": sig(size), "label": nd["entity_id"][:8],
            "role": role, "community": nd.get("community"), "pagerank": sig(nd.get("pagerank")),
            "in_n": nd.get("in_n"), "out_n": nd.get("out_n"), "in_value": sig(nd.get("in_value")),
            "out_value": sig(nd.get("out_value")), "bowtie": nd.get("bowtie_part"),
            "n_accounts": nd.get("n_accounts"), "flow_in": sig(nd.get("flow_in")), "flow_out": sig(nd.get("flow_out"))}


def write_json(path, obj) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":"), sort_keys=True))
    tmp.replace(path)
    return path.stat().st_size


def graph_json(month, asset, kind, nodes, edges, extra=None) -> dict:
    return {"attributes": {"month": month, "asset": asset, "asset_label": asset_label(asset), "kind": kind,
                           "graph_version": GRAPH_VERSION, "views_version": VIEWS_VERSION, **(extra or {})},
            "options": {"type": "directed", "multi": False, "allowSelfLoops": False},
            "nodes": nodes, "edges": edges}


def edge_size(w: np.ndarray) -> np.ndarray:
    top = math.log1p(float(w.max())) if len(w) and w.max() > 0 else 1.0
    return 0.5 + 3.5 * np.log1p(np.maximum(w, 0)) / top


def grow(seed: int, src: np.ndarray, dst: np.ndarray, w: np.ndarray, limit: int) -> list[int]:
    """A connected group around `seed`: repeatedly add the outside vertex with the most weight to the group.

    Ties are broken by vertex id, so the result is deterministic. Stops early when nothing else is reachable.
    """
    if len(src) == 0:
        return [seed]
    ends = np.concatenate([src, dst]).astype(np.int64)
    other = np.concatenate([dst, src]).astype(np.int64)
    ww = np.concatenate([w, w])
    order = np.argsort(ends, kind="stable")
    ends, other, ww = ends[order], other[order], ww[order]
    nv = int(max(ends.max(), seed) + 1)
    start = np.searchsorted(ends, np.arange(nv + 1))
    score: dict[int, float] = {}
    group, inside = [seed], {seed}
    heap: list[tuple[float, int]] = []

    def add(v):
        lo, hi = start[v], start[v + 1]
        for u, x in zip(other[lo:hi].tolist(), ww[lo:hi].tolist()):
            if u not in inside:
                score[u] = score.get(u, 0.0) + x
                heapq.heappush(heap, (-score[u], u))

    add(seed)
    while heap and len(group) < limit:
        s, u = heapq.heappop(heap)
        if u in inside or -s != score.get(u):
            continue
        group.append(u)
        inside.add(u)
        add(u)
    return group


class Sub:
    """A sub-graph on sorted global vertex ids: local edge arrays, filtered (E8) and placed (E7)."""

    def __init__(self, mg, w, ids, cap: int | None, sizes, start):
        self.ids = np.asarray(sorted(int(v) for v in ids), np.int64)
        loc = np.full(len(mg.names), -1, np.int64)
        loc[self.ids] = np.arange(len(self.ids))
        m = (loc[mg.src] >= 0) & (loc[mg.dst] >= 0)
        self.es, self.ed = loc[mg.src[m]], loc[mg.dst[m]]
        self.ew, self.en = w[m], mg.n[m]
        self.hidden = 0
        if len(self.es) > MAX_EDGES:
            keep = np.sort(np.lexsort((np.arange(len(self.ew)), -self.ew))[:MAX_EDGES])
            self.hidden = len(self.es) - MAX_EDGES
            self.es, self.ed, self.ew, self.en = self.es[keep], self.ed[keep], self.ew[keep], self.en[keep]
        self.names = [str(mg.names[v]) for v in self.ids]
        n = len(self.ids)
        if cap is None:
            self.bb, self.alpha = np.ones(len(self.es), bool), None
        else:
            self.bb, self.alpha = backbone.keep_mask(self.es, self.ed, self.ew, cap, n)
        sizes = np.asarray(sizes(self.ids) if callable(sizes) else sizes, dtype=np.float64)
        start = np.asarray(start(self.names) if callable(start) else start, dtype=np.float64)
        self.xy, self.sizes, self.n_overlaps = layout.place(n, self.es[self.bb], self.ed[self.bb], self.ew[self.bb],
                                                            sizes, start)
        self.ranks = layout.label_ranks(self.sizes, self.names)

    def pairs(self, asset) -> list[tuple]:
        """Edges drawn by default (bb); sample hashes are looked up for these only."""
        return [(self.names[a], self.names[b], asset) for a, b in zip(self.es[self.bb], self.ed[self.bb])]

    def view(self, month, asset, kind, nodes, hashes=None, node_extra=None, extra=None) -> dict:
        jn = []
        for i, nm in enumerate(self.names):
            a = node_attrs(nodes[nm], self.xy[i][0], self.xy[i][1], self.sizes[i])
            a["label_rank"] = self.ranks[i]
            if node_extra:
                a.update(node_extra(i, nm))
            jn.append({"key": nm, "attributes": a})
        sz = edge_size(self.ew)
        je = []
        for a, b, x, k, s, keep in zip(self.es, self.ed, self.ew, self.en, sz, self.bb):
            src, dst = self.names[a], self.names[b]
            at = {"size": sig(s), "n": int(k), "value": sig(x) if asset != ALL else None, "bb": int(keep)}
            if hashes is not None and keep:
                at["hashes"] = hashes.get((src, dst, asset), [])
            je.append({"key": f"{src}>{dst}", "source": src, "target": dst, "attributes": at})
        return graph_json(month, asset, kind, jn, je, {
            "layout": f"{layout.ALGORITHM}+noverlap", "n_overlaps": self.n_overlaps,
            "n_backbone_edges": int(self.bb.sum()), "bb_alpha": self.alpha, "n_hidden_edges": self.hidden,
            **(extra or {})})


def build(con, p: Paths, out_dir, month: str) -> list[dict]:
    """Write the views for one month; return index entries."""
    index = []
    for asset in VIEW_ASSETS:
        lab = asset_label(asset)
        with Timer(f"{month} views {lab}"):
            mg = load_month(con, p, month, asset, level="entity")
            if not len(mg.src):
                continue
            nodes = node_table(con, p, month, asset)
            w = np.nan_to_num(mg.weight, nan=0.0)
            nv = len(mg.names)
            wd = np.bincount(mg.src, weights=w, minlength=nv) + np.bincount(mg.dst, weights=w, minlength=nv)
            rank = np.lexsort((np.arange(nv), -wd))

            # backbone
            prev = load_prev(out_dir / f"backbone_{prev_month(month)}_{lab}.json")
            bb = Sub(mg, w, rank[:BACKBONE_NODES], BACKBONE_EDGES, lambda ids: layout.node_sizes(wd[ids]),
                     lambda names: start_positions(names, prev))

            # communities: map, names, and the largest communities' own views (E9, E10)
            comm = np.array([nodes[str(nm)]["community"] if nodes[str(nm)]["community"] is not None else -1
                             for nm in mg.names])
            details, names, summaries, crows = [], {}, {}, {}
            if (comm >= 0).any():
                sizes = np.bincount(comm[comm >= 0])
                big = [c for c in range(len(sizes)) if sizes[c] >= COMMUNITY_MIN_SIZE]
                crows = {r[0]: r[1:] for r in con.execute(f"""SELECT community, top_entities, size, internal_n, flows_out
                    FROM read_parquet({q(p.part('graph_communities', month))}) WHERE asset = ?""", [asset]).fetchall()}
                members: dict[int, list[dict]] = {}
                for nd in nodes.values():
                    if nd.get("community") in crows:
                        members.setdefault(nd["community"], []).append(nd)
                names = community_names({c: members.get(c, []) for c in big})
                for c in big:
                    out_total = float(sum(m.get("out_n") or 0 for m in members.get(c, [])))
                    _, csize, internal_n, flows = crows.get(c, (None, int(sizes[c]), None, None))
                    summaries[c] = community_summary(csize or int(sizes[c]), internal_n, out_total, flows, names)
                cs, cd = comm[mg.src], comm[mg.dst]
                pr = np.array([nodes[str(nm)].get("pagerank") or 0.0 for nm in mg.names])
                for c in sorted(big, key=lambda c: (-int(sizes[c]), c))[:DETAIL_TOP]:
                    inside = (cs == c) & (cd == c)
                    wdc = (np.bincount(mg.src[inside], weights=w[inside], minlength=nv)
                           + np.bincount(mg.dst[inside], weights=w[inside], minlength=nv))
                    cand = np.flatnonzero(comm == c)
                    seed = int(cand[np.lexsort((cand, -pr[cand]))[0]])     # the entity the community is named after
                    sel = grow(seed, mg.src[inside], mg.dst[inside], w[inside], DETAIL_NODES)
                    details.append((c, Sub(mg, w, sel, DETAIL_EDGES, lambda ids, wdc=wdc: layout.node_sizes(wdc[ids]),
                                           lambda names: start_positions(names, {}))))

            pairs = bb.pairs(asset) + [pr for _, d in details for pr in d.pairs(asset)]
            hashes = sample_hashes(con, p, month, pairs)

            f = f"backbone_{month}_{lab}.json"
            size = write_json(out_dir / f, bb.view(month, asset, "backbone", nodes, hashes))
            log.info("%s %s backbone: %d nodes, %d of %d edges drawn (alpha %s), %d overlaps", month, lab,
                     len(bb.names), int(bb.bb.sum()), len(bb.es), bb.alpha, bb.n_overlaps)
            index.append({"month": month, "asset": asset, "label": lab, "kind": "backbone", "file": f,
                          "n_nodes": len(bb.names), "n_edges": len(bb.es), "n_backbone_edges": int(bb.bb.sum()),
                          "bytes": size})

            if details:
                bundle = {"attributes": {"month": month, "asset": asset, "asset_label": lab, "kind": "community_detail",
                                         "graph_version": GRAPH_VERSION, "views_version": VIEWS_VERSION},
                          "communities": {f"C{c}": d.view(month, asset, "community", nodes, hashes, extra={
                              "community": c, "name": names.get(c), "summary": summaries.get(c)})
                              for c, d in details}}
                f = f"community_detail_{month}_{lab}.json"
                size = write_json(out_dir / f, bundle)
                index.append({"month": month, "asset": asset, "label": lab, "kind": "community_detail", "file": f,
                              "communities": [f"C{c}" for c, _ in details], "bytes": size})

            if names:
                big = sorted(names)
                cs, cd = comm[mg.src], comm[mg.dst]
                mm = (cs != cd) & np.isin(cs, big) & np.isin(cd, big)
                key = cs[mm].astype(np.int64) * len(sizes) + cd[mm]
                uk, inv = np.unique(key, return_inverse=True)
                fw = np.bincount(inv, weights=w[mm]) if len(uk) else np.zeros(0)
                fn = np.bincount(inv, weights=mg.n[mm]) if len(uk) else np.zeros(0)
                if len(uk) > COMMUNITY_EDGES:
                    keep = np.sort(np.lexsort((np.arange(len(fw)), -fw))[:COMMUNITY_EDGES])
                    uk, fw, fn = uk[keep], fw[keep], fn[keep]
                cpos = {c: i for i, c in enumerate(big)}
                prevc = load_prev(out_dir / f"communities_{prev_month(month)}_{lab}.json")
                cnames = [f"C{c}" for c in big]
                src = np.array([cpos[int(k // len(sizes))] for k in uk], np.int64)
                dst = np.array([cpos[int(k % len(sizes))] for k in uk], np.int64)
                csz = layout.node_sizes(np.array([sizes[c] for c in big], dtype=np.float64), 4.0, 24.0)
                xy, csz, n_over = layout.place(len(big), src, dst, fw, csz, start_positions(cnames, prevc))
                ranks = layout.label_ranks(csz, cnames)
                detail_set = {c for c, _ in details}
                jn = [{"key": cnames[i], "attributes": {
                    "x": sig(xy[i][0]), "y": sig(xy[i][1]), "size": sig(csz[i]), "label": names[c], "name": names[c],
                    "summary": summaries.get(c), "detail": f"C{c} · {int(sizes[c]):,} entities", "community": c,
                    "n_entities": int(sizes[c]), "top_entities": list(crows.get(c, ([],))[0] or []),
                    "label_rank": ranks[i], "has_detail": c in detail_set}}
                      for i, c in enumerate(big)]
                sz = edge_size(fw)
                je = [{"key": f"C{k // len(sizes)}>C{k % len(sizes)}", "source": f"C{k // len(sizes)}",
                       "target": f"C{k % len(sizes)}",
                       "attributes": {"size": sig(s), "n": int(b), "value": sig(a) if asset != ALL else None, "bb": 1}}
                      for k, a, b, s in zip(uk.tolist(), fw, fn, sz)]
                f = f"communities_{month}_{lab}.json"
                size = write_json(out_dir / f, graph_json(month, asset, "communities", jn, je, {
                    "layout": f"{layout.ALGORITHM}+noverlap", "n_overlaps": n_over}))
                index.append({"month": month, "asset": asset, "label": lab, "kind": "communities", "file": f,
                              "n_nodes": len(jn), "n_edges": len(je), "bytes": size})

            # ego views for the top entities
            g = mg.to_igraph()
            egos = []
            for v in rank[:EGO_TOP].tolist():
                ppr = np.asarray(g.personalized_pagerank(reset_vertices=[v], directed=False, damping=0.85, weights="w"))
                ppr[v] = -1
                nb = np.lexsort((np.arange(nv), -ppr))[:EGO_NEIGHBOURS]
                nb = nb[ppr[nb] > 0]
                ids = sorted([v, *nb.tolist()])
                egos.append(write_ego(out_dir, month, asset, lab, mg, w, nodes, v, ids, ppr))
            index += egos
            over = sum(e["n_overlaps"] for e in egos)
            log.info("%s %s egos: %d views, %d overlaps", month, lab, len(egos), over)
            for e in egos:
                e.pop("n_overlaps")
    return index


def write_ego(out_dir, month, asset, lab, mg, w, nodes, v, ids, ppr) -> dict:
    centre = str(mg.names[v])
    top = ppr.max() or 1.0
    sub = Sub(mg, w, ids, EGO_EDGES,
              lambda ix: np.array([14.0 if u == v else 3 + 9 * math.sqrt(max(ppr[u], 0) / top) for u in ix]),
              lambda names: start_positions(names, {centre: (0.0, 0.0)}))

    def extra(i, nm):
        u = int(sub.ids[i])
        return {"ppr": sig(float(ppr[u])) if u != v else None, "centre": u == v}

    f = f"ego_{month}_{lab}_{centre}.json"
    size = write_json(out_dir / f, sub.view(month, asset, "ego", nodes, node_extra=extra, extra={"centre": centre}))
    return {"month": month, "asset": asset, "label": lab, "kind": "ego", "file": f, "centre": centre,
            "n_nodes": len(sub.names), "n_edges": len(sub.es), "n_backbone_edges": int(sub.bb.sum()), "bytes": size,
            "n_overlaps": sub.n_overlaps}


def ego_on_demand(con, p: Paths, out_dir, month: str, asset: str, address: str) -> dict:
    """One ego view for any account (resolved to its entity)."""
    ent = con.execute(f"SELECT entity_id FROM read_parquet({q(p.part('graph_entities', month))}) WHERE account = ?",
                      [address]).fetchone()
    if not ent:
        raise SystemExit(f"{address} has no transfers in {month}")
    mg = load_month(con, p, month, asset, level="entity")
    nodes = node_table(con, p, month, asset)
    w = np.nan_to_num(mg.weight, nan=0.0)
    where = np.flatnonzero(mg.names == ent[0])
    if not len(where):
        raise SystemExit(f"{address} (entity {ent[0]}) has no {asset_label(asset)} transfers in {month}")
    v = int(where[0])
    g = mg.to_igraph()
    ppr = np.asarray(g.personalized_pagerank(reset_vertices=[v], directed=False, damping=0.85, weights="w"))
    ppr[v] = -1
    nb = np.lexsort((np.arange(len(ppr)), -ppr))[:EGO_NEIGHBOURS]
    nb = nb[ppr[nb] > 0]
    e = write_ego(out_dir, month, asset, asset_label(asset), mg, w, nodes, v, sorted([v, *nb.tolist()]), ppr)
    e.pop("n_overlaps")
    return e


def build_loops(con, p: Paths, out_dir) -> None:
    summ = sorted((p.gold / "graph_summary").glob("month=*.parquet"))
    if not summ:
        return
    rows = con.execute(f"""SELECT strftime(month, '%Y-%m') AS month, asset, asset_label, loop_share, gradient_share,
            n_cycles, reciprocity, reciprocity_weighted, n_nodes, n_edges, n_transfers, modularity, n_communities,
            giant_wcc_share, bowtie_core_share, bowtie_in_share, bowtie_out_share, bowtie_other_share,
            tail_in_alpha, tail_out_alpha, tail_value_alpha
        FROM read_parquet([{",".join(q(f) for f in summ)}], union_by_name = true) ORDER BY month, asset""")
    cols = [d[0] for d in rows.description]
    series = [{c: sig(v) if isinstance(v, float) else v for c, v in zip(cols, r)} for r in rows.fetchall()]
    last = summ[-1].stem.split("=", 1)[1]
    cyc = con.execute(f"""SELECT asset, cycle_id, length, entities, strength, sample_hashes
        FROM read_parquet({q(p.part('graph_cycles', last))}) WHERE asset IN ('XRP', '{RLUSD}', '*') AND cycle_id < 20
        ORDER BY asset, cycle_id""").fetchall() if p.part("graph_cycles", last).exists() else []
    write_json(out_dir / "loops.json", {"summary": series, "cycles_month": last, "cycles": [
        {"asset": a, "asset_label": asset_label(a), "rank": c, "length": n, "entities": e, "strength": sig(s),
         "hashes": h} for a, c, n, e, s, h in cyc]})


def write_index(out_dir, entries: list[dict]) -> None:
    old = []
    try:
        old = json.loads((out_dir / "index.json").read_text())["views"]
    except (OSError, ValueError, KeyError):
        pass
    months = {e["month"] for e in entries}
    keep = [e for e in old if e["month"] not in months and (out_dir / e["file"]).exists()]
    views = sorted(keep + entries, key=lambda e: (e["month"], e["label"], e["kind"], e.get("centre", "")))
    write_json(out_dir / "index.json", {"graph_version": GRAPH_VERSION, "views_version": VIEWS_VERSION, "views": views})
