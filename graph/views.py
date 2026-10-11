"""E5: precompute the JSON views drawn by report/graph.html (graphology serialized format, positions set).

Per month and view asset (XRP, RLUSD, all): a backbone (top entities by weighted degree), a community map,
and ego views for the top entities (personalized PageRank). Plus loops.json (Hodge loop share over time and
the strongest cycles) and index.json (what exists). The browser never runs a layout.
"""

import json
import logging
import math
import random

import igraph as ig
import numpy as np

from common import ALL, GRAPH_VERSION, RLUSD, SEED, Paths, Timer, asset_label, prev_month, q
from load import load_month
from metrics import sample_hashes

log = logging.getLogger("graph")

VIEW_ASSETS = ("XRP", RLUSD, ALL)
BACKBONE_NODES, MAX_EDGES, COMMUNITY_MIN_SIZE, COMMUNITY_EDGES = 300, 20_000, 20, 2_000
EGO_TOP, EGO_NEIGHBOURS = 100, 50
LAYOUT_ITERS = 1000


def sig(x, d: int = 6):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    if isinstance(x, (int, np.integer)):
        return int(x)
    return float(f"{float(x):.{d}g}")


def layout(n: int, edges: list[tuple[int, int]], weights: list[float], start: list[tuple[float, float]]) -> np.ndarray:
    """Fruchterman-Reingold from given starting positions, deterministic, scaled to [-1, 1]."""
    if n == 0:
        return np.zeros((0, 2))
    ig.set_random_number_generator(random.Random(SEED))
    g = ig.Graph(n=n, edges=edges, directed=False)
    w = [math.log1p(max(x, 0.0)) + 0.1 for x in weights] if edges else None
    pos = np.asarray(g.layout_fruchterman_reingold(weights=w, niter=LAYOUT_ITERS, seed=[list(s) for s in start]).coords)
    pos -= pos.mean(axis=0)
    span = np.abs(pos).max() or 1.0
    return pos / span


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
    return {"x": sig(x), "y": sig(y), "size": sig(size), "label": f"{nd['entity_id'][:8]} · {role}",
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
                           "graph_version": GRAPH_VERSION, **(extra or {})},
            "options": {"type": "directed", "multi": False, "allowSelfLoops": False},
            "nodes": nodes, "edges": edges}


def edge_size(w: np.ndarray) -> np.ndarray:
    top = math.log1p(float(w.max())) if len(w) and w.max() > 0 else 1.0
    return 0.5 + 3.5 * np.log1p(np.maximum(w, 0)) / top


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
            sel = rank[:BACKBONE_NODES]
            pos_of = {int(v): i for i, v in enumerate(sorted(sel.tolist()))}
            m = np.isin(mg.src, sel) & np.isin(mg.dst, sel)
            es, ed, ew, en = mg.src[m], mg.dst[m], w[m], mg.n[m]
            hidden = 0
            if len(es) > MAX_EDGES:
                keep = np.lexsort((np.arange(len(ew)), -ew))[:MAX_EDGES]
                keep.sort()
                hidden = len(es) - MAX_EDGES
                es, ed, ew, en = es[keep], ed[keep], ew[keep], en[keep]
            names = [str(mg.names[v]) for v in sorted(sel.tolist())]
            prev = load_prev(out_dir / f"backbone_{prev_month(month)}_{lab}.json")
            xy = layout(len(names), [(pos_of[int(a)], pos_of[int(b)]) for a, b in zip(es, ed)], ew.tolist(),
                        start_positions(names, prev))
            hashes = sample_hashes(con, p, month, [(str(mg.names[a]), str(mg.names[b]), asset) for a, b in zip(es, ed)])
            top_wd = wd[sel].max() or 1.0
            jn = [{"key": nm, "attributes": node_attrs(nodes[nm], xy[i][0], xy[i][1],
                                                       2 + 18 * math.sqrt(wd[v] / top_wd))}
                  for i, (nm, v) in enumerate(zip(names, sorted(sel.tolist())))]
            sz = edge_size(ew)
            je = [{"key": f"{mg.names[a]}>{mg.names[b]}", "source": str(mg.names[a]), "target": str(mg.names[b]),
                   "attributes": {"size": sig(s), "n": int(k), "value": sig(x) if asset != ALL else None,
                                  "hashes": hashes.get((str(mg.names[a]), str(mg.names[b]), asset), [])}}
                  for a, b, x, k, s in zip(es, ed, ew, en, sz)]
            f = f"backbone_{month}_{lab}.json"
            size = write_json(out_dir / f, graph_json(month, asset, "backbone", jn, je, {"n_hidden_edges": hidden}))
            index.append({"month": month, "asset": asset, "label": lab, "kind": "backbone", "file": f,
                          "n_nodes": len(jn), "n_edges": len(je), "bytes": size})

            # community map
            comm = np.array([nodes[str(nm)]["community"] if nodes[str(nm)]["community"] is not None else -1
                             for nm in mg.names])
            if (comm >= 0).any():
                sizes = np.bincount(comm[comm >= 0])
                big = [c for c in range(len(sizes)) if sizes[c] >= COMMUNITY_MIN_SIZE]
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
                prev = load_prev(out_dir / f"communities_{prev_month(month)}_{lab}.json")
                cnames = [f"C{c}" for c in big]
                pairs = [(cpos[int(k // len(sizes))], cpos[int(k % len(sizes))]) for k in uk]
                xy = layout(len(big), pairs, fw.tolist(), start_positions(cnames, prev))
                top = con.execute(f"""SELECT community, top_entities FROM read_parquet({q(p.part('graph_communities', month))})
                    WHERE asset = ?""", [asset]).fetchall()
                top = dict(top)
                smax = float(sizes[big].max()) if big else 1.0
                jn = [{"key": cnames[i], "attributes": {"x": sig(xy[i][0]), "y": sig(xy[i][1]),
                                                       "size": sig(3 + 22 * math.sqrt(sizes[c] / smax)),
                                                       "label": f"C{c} · {int(sizes[c]):,} entities", "community": c,
                                                       "n_entities": int(sizes[c]), "top_entities": top.get(c, [])}}
                      for i, c in enumerate(big)]
                sz = edge_size(fw)
                je = [{"key": f"C{k // len(sizes)}>C{k % len(sizes)}", "source": f"C{k // len(sizes)}",
                       "target": f"C{k % len(sizes)}",
                       "attributes": {"size": sig(s), "n": int(b), "value": sig(a) if asset != ALL else None}}
                      for k, a, b, s in zip(uk.tolist(), fw, fn, sz)]
                f = f"communities_{month}_{lab}.json"
                size = write_json(out_dir / f, graph_json(month, asset, "communities", jn, je))
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
    return index


def write_ego(out_dir, month, asset, lab, mg, w, nodes, v, ids, ppr) -> dict:
    pos_of = {u: i for i, u in enumerate(ids)}
    m = np.isin(mg.src, ids) & np.isin(mg.dst, ids)
    es, ed, ew, en = mg.src[m], mg.dst[m], w[m], mg.n[m]
    names = [str(mg.names[u]) for u in ids]
    start = start_positions(names, {str(mg.names[v]): (0.0, 0.0)})
    xy = layout(len(ids), [(pos_of[int(a)], pos_of[int(b)]) for a, b in zip(es, ed)], ew.tolist(), start)
    centre = str(mg.names[v])
    jn = []
    for i, (u, nm) in enumerate(zip(ids, names)):
        a = node_attrs(nodes[nm], xy[i][0], xy[i][1], 14 if u == v else 3 + 9 * math.sqrt(max(ppr[u], 0) / (ppr.max() or 1)))
        a["ppr"] = sig(float(ppr[u])) if u != v else None
        a["centre"] = u == v
        jn.append({"key": nm, "attributes": a})
    sz = edge_size(ew)
    je = [{"key": f"{mg.names[a]}>{mg.names[b]}", "source": str(mg.names[a]), "target": str(mg.names[b]),
           "attributes": {"size": sig(s), "n": int(k), "value": sig(x) if asset != ALL else None}}
          for a, b, x, k, s in zip(es, ed, ew, en, sz)]
    f = f"ego_{month}_{lab}_{centre}.json"
    size = write_json(out_dir / f, graph_json(month, asset, "ego", jn, je, {"centre": centre}))
    return {"month": month, "asset": asset, "label": lab, "kind": "ego", "file": f, "centre": centre,
            "n_nodes": len(jn), "n_edges": len(je), "bytes": size}


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
    return write_ego(out_dir, month, asset, asset_label(asset), mg, w, nodes, v, sorted([v, *nb.tolist()]), ppr)


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
    write_json(out_dir / "index.json", {"graph_version": GRAPH_VERSION, "views": views})
