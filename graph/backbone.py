"""E8: keep the edges that matter to each node (disparity filter, Serrano, Boguna and Vespignani 2009).

Edges are treated as undirected for the test. For node i with k_i incident edges and strength s_i, an edge
of weight w has share p = w / s_i and significance alpha = (1 - p) ** (k_i - 1). An edge is kept when alpha is
below the threshold at either endpoint. Every node also keeps its strongest edge, and a maximum spanning forest
is always kept, so a connected view stays connected (no fragments drifting apart in the layout).
"""

import numpy as np

ALPHAS = (0.05, 0.02, 0.01, 0.005, 0.001)


def significance(src: np.ndarray, dst: np.ndarray, w: np.ndarray, n: int) -> np.ndarray:
    """min over both endpoints of the disparity alpha, per edge (0 = most significant)."""
    w = np.maximum(np.nan_to_num(np.asarray(w, dtype=np.float64)), 0.0)
    k = np.bincount(src, minlength=n) + np.bincount(dst, minlength=n)
    s = np.bincount(src, weights=w, minlength=n) + np.bincount(dst, weights=w, minlength=n)

    def alpha(v):
        p = np.divide(w, s[v], out=np.zeros_like(w), where=s[v] > 0)
        a = np.power(np.clip(1.0 - p, 0.0, 1.0), np.maximum(k[v] - 1, 0))
        return np.where(k[v] <= 1, 0.0, a)

    return np.minimum(alpha(src), alpha(dst))


def strongest(src: np.ndarray, dst: np.ndarray, w: np.ndarray, n: int) -> np.ndarray:
    """Mask of each node's heaviest incident edge (ties: lowest edge index)."""
    m = len(src)
    keep = np.zeros(m, bool)
    if m == 0:
        return keep
    w = np.nan_to_num(np.asarray(w, dtype=np.float64))
    ends = np.concatenate([src, dst])
    idx = np.concatenate([np.arange(m), np.arange(m)])
    ww = np.concatenate([w, w])
    order = np.lexsort((idx, -ww, ends))               # by node, heaviest first, then edge index
    first = np.ones(len(order), bool)
    first[1:] = ends[order][1:] != ends[order][:-1]
    keep[idx[order][first]] = True
    return keep


def spanning_forest(src: np.ndarray, dst: np.ndarray, w: np.ndarray, n: int) -> np.ndarray:
    """Mask of a maximum-weight spanning forest (Kruskal; ties by edge index), edges taken as undirected."""
    keep = np.zeros(len(src), bool)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    w = np.nan_to_num(np.asarray(w, dtype=np.float64))
    for e in np.lexsort((np.arange(len(src)), -w)).tolist():
        a, b = find(int(src[e])), find(int(dst[e]))
        if a != b:
            parent[a] = b
            keep[e] = True
    return keep


def keep_mask(src, dst, w, cap: int, n: int | None = None) -> tuple[np.ndarray, float]:
    """(mask of kept edges, alpha threshold used). At most `cap` edges unless the forest + strongest set is larger."""
    src, dst = np.asarray(src, np.int64), np.asarray(dst, np.int64)
    m = len(src)
    if m == 0:
        return np.zeros(0, bool), ALPHAS[0]
    n = n or int(max(src.max(), dst.max()) + 1)
    a = significance(src, dst, w, n)
    base = strongest(src, dst, w, n) | spanning_forest(src, dst, w, n)
    for thr in ALPHAS:
        mask = base | (a < thr)
        if mask.sum() <= cap:
            return mask, thr
    mask = base.copy()
    room = cap - int(mask.sum())
    if room > 0:
        rest = np.flatnonzero(~mask)
        mask[rest[np.lexsort((rest, a[rest]))[:room]]] = True
    return mask, 0.0
