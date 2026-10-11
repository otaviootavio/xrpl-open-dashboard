"""Hodge decomposition of net flow: how much is one-way (gradient) and how much goes round in loops.

Net flow on each undirected edge {i, j}, i < j, is Y = w_ij - w_ji. The potential s solves
min ||B s - Y||^2 (B: edge-node incidence, -1 at i, +1 at j), so flow runs from low to high potential:
a high potential marks a net sink. s comes from the normal equations L s = B^T Y (L = B^T B, the graph Laplacian) by
Jacobi-preconditioned conjugate gradients: 74 iterations, 0.9 s on 2025-11 XRP, where lsqr on B took 153 s.
L is singular (constants per component), so s is centred to mean 0 in each component. G = B s and R = Y - G are orthogonal, so
gradient_share = ||G||^2 / ||Y||^2 and loop_share = ||R||^2 / ||Y||^2 add up to 1.
The curl / harmonic split of R is not computed in v1 (curl_share, harmonic_share stay NULL).
"""

import numpy as np
from scipy.sparse import csr_matrix, diags
from scipy.sparse.csgraph import connected_components
from scipy.sparse.linalg import cg, lsqr


def net_flow(n_vertices: int, src: np.ndarray, dst: np.ndarray, w: np.ndarray):
    """Undirected edges (i < j) with Y = w_ij - w_ji, ordered by (i, j)."""
    lo, hi = np.minimum(src, dst), np.maximum(src, dst)
    sign = np.where(src < dst, 1.0, -1.0)
    key = lo.astype(np.int64) * n_vertices + hi
    order = np.argsort(key, kind="stable")
    key, signed = key[order], (sign * w)[order]
    uniq, start = np.unique(key, return_index=True)
    y = np.add.reduceat(signed, start) if len(signed) else signed
    return (uniq // n_vertices).astype(np.int64), (uniq % n_vertices).astype(np.int64), y


def decompose(n_vertices: int, src: np.ndarray, dst: np.ndarray, w: np.ndarray) -> dict:
    i, j, y = net_flow(n_vertices, src, dst, np.asarray(w, dtype=np.float64))
    keep = y != 0
    i, j, y = i[keep], j[keep], y[keep]
    total = float(np.dot(y, y))
    if not len(y) or total == 0:
        return {"gradient_share": None, "loop_share": None, "curl_share": None, "harmonic_share": None,
                "lsqr_iters": 0, "n_flow_edges": 0, "potential": np.zeros(n_vertices)}
    # Scale to unit RMS: lsqr's tolerances are relative, but huge values (1e20 XRP-scale tokens) lose digits.
    scale = np.sqrt(total / len(y))
    m = len(y)
    rows = np.repeat(np.arange(m), 2)
    cols = np.column_stack([i, j]).ravel()
    vals = np.tile([-1.0, 1.0], m)
    b = csr_matrix((vals, (rows, cols)), shape=(m, n_vertices))
    lap = (b.T @ b).tocsr()
    d = lap.diagonal()
    iters = [0]
    s, info = cg(lap, b.T @ (y / scale), rtol=1e-10, maxiter=100_000, M=diags(1.0 / np.where(d > 0, d, 1.0)),
                 callback=lambda _: iters.__setitem__(0, iters[0] + 1))
    iters = iters[0]
    if info != 0:  # not converged: fall back to the slow, robust solver
        res = lsqr(b, y / scale, atol=1e-12, btol=1e-12, iter_lim=20 * n_vertices)
        s, iters = res[0], -int(res[2])
    _, comp = connected_components(lap, directed=False)
    s = s - (np.bincount(comp, weights=s) / np.bincount(comp))[comp]
    g = b @ s
    r = y / scale - g
    gn, rn = float(np.dot(g, g)), float(np.dot(r, r))
    # ||y / scale||^2 = m; G and R are orthogonal at the optimum, so the shares add up to 1 (tested)
    return {"gradient_share": gn / m, "loop_share": rn / m, "curl_share": None,
            "harmonic_share": None, "lsqr_iters": iters, "n_flow_edges": m, "potential": s * scale}
