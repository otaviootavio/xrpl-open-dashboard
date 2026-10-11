"""E7: deterministic layouts for the precomputed views, with no node overlaps at the page's default zoom.

Sigma.js fits the graph's bounding box into the stage and draws node sizes in screen pixels, so overlap is
judged in pixels: once coordinates are normalised (largest extent spans [-1, 1]) one unit is PX_PER_UNIT
pixels on the reference 1,000 x 700 stage with sigma's 30 px padding.

    force_layout     ForceAtlas2 (LinLog attraction) or igraph Fruchterman-Reingold, from given starts
    remove_overlaps  push overlapping discs apart in the normalised frame, shrinking sizes only if needed
    place            both, the one call views.py makes
    check_view       overlap and label-collision counts for a written view (tests and logs)
"""

import math
import random

import igraph as ig
import numpy as np

from common import SEED

PX_PER_UNIT = (700 - 2 * 30) / 2       # reference stage 1,000 x 700, sigma stagePadding 30
GAP_PX = 2.0                           # clear space kept between two node discs
LABELS_DEFAULT = 30                    # labels shown at default zoom (graph.html reads label_rank)
LABEL_PX_PER_CHAR, LABEL_PX_HEIGHT = 6.5, 13.0
ALGORITHM = "fa2"                      # "fa2" or "fr"; chosen in docs/graph-layout-spike.md


def node_sizes(metric: np.ndarray, lo: float = 3.0, hi: float = 18.0) -> np.ndarray:
    """Square-root scale into [lo, hi] px, so the largest node is at most hi/lo times the smallest."""
    m = np.maximum(np.nan_to_num(np.asarray(metric, dtype=np.float64)), 0.0)
    top = m.max() if len(m) else 0.0
    return lo + (hi - lo) * np.sqrt(m / top) if top > 0 else np.full(len(m), lo)


def normalise(pos: np.ndarray) -> np.ndarray:
    """Centre on the bounding box and scale its larger side to [-1, 1], as sigma's camera does."""
    if len(pos) == 0:
        return pos
    lo, hi = pos.min(axis=0), pos.max(axis=0)
    half = float((hi - lo).max()) / 2 or 1.0
    return (pos - (lo + hi) / 2) / half


def _fr(n, src, dst, w, start, iters) -> np.ndarray:
    ig.set_random_number_generator(random.Random(SEED))
    g = ig.Graph(n=n, edges=list(zip(src.tolist(), dst.tolist())), directed=False)
    ww = (np.log1p(np.maximum(w, 0.0)) + 0.1).tolist() if len(src) else None
    return np.asarray(g.layout_fruchterman_reingold(weights=ww, niter=iters, seed=start.tolist()).coords)


def _fa2(n, src, dst, w, start, iters) -> np.ndarray:
    """ForceAtlas2 (Jacomy et al. 2014): degree-weighted repulsion, LinLog attraction, gravity, adaptive speed."""
    pos = start * math.sqrt(n) * 2.0
    deg = np.bincount(src, minlength=n) + np.bincount(dst, minlength=n)
    mass = deg + 1.0
    kr, kg = (10.0 if n < 100 else 2.0), 1.0
    ew = np.log1p(np.maximum(w, 0.0)) + 0.1
    ew = ew / ew.mean() if len(ew) else ew
    mm = np.outer(mass, mass)
    np.fill_diagonal(mm, 0.0)
    prev = np.zeros_like(pos)
    speed, speed_eff = 1.0, 1.0
    for _ in range(iters):
        delta = pos[:, None, :] - pos[None, :, :]
        d2 = np.einsum("ijk,ijk->ij", delta, delta)
        np.fill_diagonal(d2, 1.0)
        d2 = np.maximum(d2, 1e-6)
        force = np.einsum("ij,ijk->ik", kr * mm / d2, delta)          # kr*mi*mj/d along the unit vector
        if len(src):
            v = pos[dst] - pos[src]
            d = np.sqrt(np.einsum("ij,ij->i", v, v))
            f = (ew * np.log1p(d) / np.maximum(d, 1e-9))[:, None] * v   # LinLog, pulls src towards dst
            np.add.at(force, src, f)
            np.add.at(force, dst, -f)
        r = np.sqrt(np.einsum("ij,ij->i", pos, pos))
        force -= (kg * mass / np.maximum(r, 1e-9))[:, None] * pos
        swing = np.sqrt(np.einsum("ij,ij->i", force - prev, force - prev))
        traction = np.sqrt(np.einsum("ij,ij->i", force + prev, force + prev)) / 2
        gs, gt = float((mass * swing).sum()), float((mass * traction).sum())
        jt = 0.05 * math.sqrt(n) if n > 1 else 1.0
        target = jt * gt / gs if gs > 0 else speed
        speed_eff = min(target, 1.5 * speed_eff) if gs > 0 else speed_eff
        speed = speed_eff
        fnorm = np.sqrt(np.einsum("ij,ij->i", force, force))
        s = np.minimum(0.1 * speed / (1.0 + speed * np.sqrt(swing)), 10.0 / np.maximum(fnorm, 1e-9))
        pos = pos + s[:, None] * force
        prev = force
    return pos


def force_layout(n: int, src: np.ndarray, dst: np.ndarray, w: np.ndarray, start: np.ndarray,
                 iters: int | None = None, algorithm: str = ALGORITHM) -> np.ndarray:
    """Positions for n nodes from start positions (n x 2); deterministic for the same input."""
    if n == 0:
        return np.zeros((0, 2))
    src, dst = np.asarray(src, np.int64), np.asarray(dst, np.int64)
    w = np.nan_to_num(np.asarray(w, dtype=np.float64))
    start = np.asarray(start, dtype=np.float64).reshape(n, 2)
    if n == 1:
        return np.zeros((1, 2))
    iters = iters or (1000 if n > 100 else 400)
    return (_fa2 if algorithm == "fa2" else _fr)(n, src, dst, w, start, iters)


def _overlaps(pos: np.ndarray, r: np.ndarray, gap: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    delta = pos[:, None, :] - pos[None, :, :]
    d = np.sqrt(np.einsum("ijk,ijk->ij", delta, delta))
    need = r[:, None] + r[None, :] + gap
    over = np.triu(need - d > 1e-12, 1)
    return over, delta, d


def remove_overlaps(pos: np.ndarray, sizes_px: np.ndarray, gap_px: float = GAP_PX, max_passes: int = 500,
                    max_rounds: int = 10) -> tuple[np.ndarray, np.ndarray, int]:
    """Normalised positions with no two discs closer than gap_px at the reference stage.

    Returns (positions, sizes, overlaps left). Sizes come back unchanged unless the discs needed more area
    than the stage has, in which case they are scaled down by 0.9 steps until a layout fits.
    """
    n = len(pos)
    sizes = np.asarray(sizes_px, dtype=np.float64).copy()
    if n < 2:
        return normalise(pos), sizes, 0
    ang = 2 * math.pi * (np.arange(n) % 360) / 360
    base = normalise(np.asarray(pos, dtype=np.float64))
    for _ in range(30):                                   # size steps: 0.9 ** 30 ~ 4% at worst
        p = base.copy()
        r, gap = sizes / PX_PER_UNIT, gap_px / PX_PER_UNIT
        for _ in range(max_rounds):
            p = normalise(p)
            over, _, _ = _overlaps(p, r, gap)
            if not over.any():
                return p, sizes, 0
            for _ in range(max_passes):
                over, delta, d = _overlaps(p, r, gap)
                if not over.any():
                    break
                i, j = np.nonzero(over)
                need = r[i] + r[j] + gap
                dd = d[i, j]
                u = np.where(dd[:, None] > 1e-12, delta[i, j] / np.maximum(dd, 1e-12)[:, None],
                             np.column_stack([np.cos(ang[i]), np.sin(ang[i])]))
                push = ((need - dd) / 2 + 1e-9)[:, None] * u
                disp = np.zeros_like(p)
                np.add.at(disp, i, push)
                np.add.at(disp, j, -push)
                p = p + disp
        p = normalise(p)
        if not _overlaps(p, sizes / PX_PER_UNIT, gap_px / PX_PER_UNIT)[0].any():
            return p, sizes, 0
        sizes = sizes * 0.9
    p = normalise(p)
    return p, sizes, int(_overlaps(p, sizes / PX_PER_UNIT, gap_px / PX_PER_UNIT)[0].sum())


def place(n: int, src, dst, w, sizes_px, start, iters: int | None = None,
          algorithm: str = ALGORITHM) -> tuple[np.ndarray, np.ndarray, int]:
    """Force layout then overlap removal: (normalised positions, sizes in px, overlaps left)."""
    pos = force_layout(n, np.asarray(src), np.asarray(dst), np.asarray(w), np.asarray(start), iters, algorithm)
    return remove_overlaps(pos, np.asarray(sizes_px, dtype=np.float64))


def label_ranks(sizes: np.ndarray, keys: list[str]) -> list[int]:
    """0 = largest node; ties broken by key."""
    order = sorted(range(len(keys)), key=lambda i: (-float(sizes[i]), keys[i]))
    rank = [0] * len(keys)
    for k, i in enumerate(order):
        rank[i] = k
    return rank


def check_view(view: dict, n_labels: int = LABELS_DEFAULT) -> dict:
    """Overlapping node pairs and colliding label boxes at the default camera of the reference stage."""
    nodes = [nd["attributes"] for nd in view.get("nodes", [])]
    if not nodes:
        return {"n_overlaps": 0, "n_label_collisions": 0}
    pos = normalise(np.array([[a["x"], a["y"]] for a in nodes], dtype=np.float64)) * PX_PER_UNIT
    s = np.array([a.get("size") or 1.0 for a in nodes], dtype=np.float64)
    delta = pos[:, None, :] - pos[None, :, :]
    d = np.sqrt(np.einsum("ijk,ijk->ij", delta, delta))
    n_over = int(np.triu(s[:, None] + s[None, :] - d > 0.5, 1).sum())
    boxes = []
    for a, (x, y), sz in zip(nodes, pos, s):
        if a.get("label") and a.get("label_rank", 0) < n_labels:
            x0 = x + sz + 3
            boxes.append((x0, y - LABEL_PX_HEIGHT / 2, x0 + LABEL_PX_PER_CHAR * len(a["label"]), y + LABEL_PX_HEIGHT / 2))
    coll = 0
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i], boxes[j]
            if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                coll += 1
    return {"n_overlaps": n_over, "n_label_collisions": coll}
