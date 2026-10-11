"""Power-law tail fits (Clauset, Shalizi and Newman 2009) on a fixed grid of x_min candidates.

powerlaw.Fit's own x_min search takes minutes on a month of edges, so x_min is chosen here by the smallest
KS distance over 40 quantile candidates; powerlaw then fits alpha at that x_min and compares it with a
lognormal. alpha is the density exponent; the Pareto index of the cumulative distribution is alpha - 1.
"""

import warnings

import numpy as np

CANDIDATES = 40
MIN_TAIL = 50


def _alpha(tail: np.ndarray, xmin: float, discrete: bool) -> float:
    base = xmin - 0.5 if discrete else xmin
    return 1.0 + len(tail) / float(np.sum(np.log(tail / base)))


def _ks(tail: np.ndarray, xmin: float, alpha: float) -> float:
    x = np.sort(tail)
    emp = np.arange(1, len(x) + 1) / len(x)
    model = 1.0 - (x / xmin) ** (1.0 - alpha)
    return float(np.max(np.abs(emp - model)))


def fit(x, discrete: bool) -> dict:
    x = np.asarray(x, dtype=np.float64)
    x = x[np.isfinite(x) & (x > 0)]
    out = {"alpha": None, "xmin": None, "n_tail": 0, "ks": None, "R_lognormal": None, "p_lognormal": None}
    if len(x) < MIN_TAIL:
        return out
    cands = np.unique(np.quantile(x, np.linspace(0.0, 0.99, CANDIDATES), method="lower"))
    best = None
    for xmin in cands:
        tail = x[x >= xmin]
        if len(tail) < MIN_TAIL:
            break
        a = _alpha(tail, xmin, discrete)
        if not np.isfinite(a) or a <= 1:
            continue
        d = _ks(tail, xmin, a)
        if best is None or d < best[0]:
            best = (d, float(xmin), a, len(tail))
    if best is None:
        return out
    d, xmin, a, n = best
    out.update(alpha=a, xmin=xmin, n_tail=n, ks=d)
    try:
        import powerlaw
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            f = powerlaw.Fit(x, xmin=xmin, discrete=discrete, verbose=False)
            out["alpha"] = float(f.power_law.alpha)
            r, p = f.distribution_compare("power_law", "lognormal", normalized_ratio=True)
            out["R_lognormal"], out["p_lognormal"] = float(r), float(p)
    except Exception:  # the comparison is informative only; keep our own alpha
        pass
    return out
