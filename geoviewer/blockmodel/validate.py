"""Validation of estimated block values: the usual checks before an estimate is used.

1. Cross-validation - every sample (all of them, or a random few thousand) is estimated
   from the other samples with the same method and parameters, leaving out its whole
   drillhole (the way blocks between holes are estimated). Compared with the true values:
   mean error (bias), RMSE, correlation, and the slope of the regression of the true values
   on the estimates (near 1: little conditional bias; below 1: the estimates are too
   spread out, above 1: too smooth).
2. Global bias - the mean of the estimate over the estimated blocks against a nearest
   neighbour estimate of the same blocks (a declustered reference: every sample stands for
   the volume nearest to it) and against the plain sample mean.
3. Smoothing - the spread (CV) of the block values against that of the samples and of the
   nearest neighbour model: kriging smooths, but a block CV far below the samples' means
   too much smoothing (too many samples / too long a search).
4. Swath plots - along X, Y and Z, slices of the model: mean of the estimate, of the
   nearest neighbour model and of the samples in each slice. The estimate should follow
   the samples' trend without bias.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..i18n import tr
from .estimate import EstimationParams, _cv_estimates, _cv_neighbours, estimate

GOOD_BIAS = 5.0        # % of the reference mean: green below, amber to 2x, red beyond
GOOD_SLOPE = (0.8, 1.2)


@dataclass
class CrossValidation:
    true: np.ndarray
    est: np.ndarray            # NaN where not estimated
    hole: np.ndarray
    stats: dict


def cross_validation(samples, p: EstimationParams, n_test: int = 3000, seed: int = 0) -> CrossValidation:
    test, idx, dist = _cv_neighbours(samples, p.search, p.radius * (p.pass_factor ** (max(1, p.passes) - 1)),
                                     p.max_samples, p.max_per_hole, n_test, seed)
    est = np.full(len(test), np.nan)
    for pas in range(max(1, min(3, int(p.passes)))):                 # the same passes as the estimate
        r = p.radius * (p.pass_factor ** pas)
        todo = ~np.isfinite(est)
        if not todo.any():
            break
        e = _cv_estimates(samples, p, test[todo], idx[todo], dist[todo], r, p.max_samples, p.min_samples)
        est[np.flatnonzero(todo)] = e
    true = samples.value[test]
    return CrossValidation(true, est, samples.hole[test], cv_stats(true, est))


def cv_stats(true: np.ndarray, est: np.ndarray) -> dict:
    ok = np.isfinite(est)
    n = int(ok.sum())
    out = {"n": n, "tested": int(len(est)), "cover": n / max(len(est), 1)}
    if n < 3:
        return out
    t, e = true[ok], est[ok]
    err = e - t
    ve = float(np.var(e))
    out.update(mean_true=float(t.mean()), mean_est=float(e.mean()), me=float(err.mean()),
               mae=float(np.abs(err).mean()), rmse=float(np.sqrt(np.mean(err * err))),
               corr=float(np.corrcoef(t, e)[0, 1]) if ve > 0 and np.var(t) > 0 else 0.0,
               slope=float(np.cov(t, e)[0, 1] / ve) if ve > 0 else float("nan"),
               bias_pct=float(100 * err.mean() / t.mean()) if t.mean() else float("nan"))
    return out


def nn_params(p: EstimationParams) -> EstimationParams:
    """The nearest neighbour reference's parameters: the same search ellipsoid and passes."""
    return EstimationParams(**{**p.__dict__, "method": "nn", "min_samples": 1, "max_samples": 1, "max_per_hole": 0})


def nn_reference(centroids, sizes, samples, p: EstimationParams) -> np.ndarray:
    """Nearest neighbour values at the blocks with the same search ellipsoid (radius
    enlarged like the last pass), NaN where no sample is in reach."""
    return estimate(centroids, sizes, samples, nn_params(p), ("value",))["value"]


def _wmean(v, w):
    m = np.isfinite(v)
    return float(np.average(v[m], weights=w[m])) if m.any() and w[m].sum() > 0 else float("nan")


def _wcv(v, w):
    m = np.isfinite(v)
    if not m.any():
        return float("nan")
    mean = np.average(v[m], weights=w[m])
    sd = np.sqrt(np.average((v[m] - mean) ** 2, weights=w[m]))
    return float(sd / mean) if mean else float("nan")


@dataclass
class GlobalCheck:
    mean_est: float
    mean_nn: float
    mean_samples: float
    bias_pct: float            # estimate vs nearest neighbour, % of the nearest neighbour mean
    cv_est: float
    cv_nn: float
    cv_samples: float
    n_blocks: int


def global_check(values: np.ndarray, nn: np.ndarray, volume: np.ndarray, samples) -> GlobalCheck:
    both = np.isfinite(values) & np.isfinite(nn)
    w = volume
    me = _wmean(np.where(both, values, np.nan), w)
    mn = _wmean(np.where(both, nn, np.nan), w)
    ms = float(np.average(samples.value, weights=samples.length))
    bias = 100.0 * (me - mn) / mn if mn else float("nan")
    cvs = float(np.sqrt(np.average((samples.value - ms) ** 2, weights=samples.length)) / ms) if ms else float("nan")
    return GlobalCheck(me, mn, ms, bias, _wcv(np.where(both, values, np.nan), w),
                       _wcv(np.where(both, nn, np.nan), w), cvs, int(both.sum()))


@dataclass
class Swath:
    axis: int
    centre: np.ndarray         # slice centres (m)
    est: np.ndarray            # mean estimate per slice (volume weighted)
    nn: np.ndarray             # mean nearest neighbour value per slice
    samples: np.ndarray        # mean sample value per slice (length weighted)
    n_samples: np.ndarray
    volume: np.ndarray         # estimated volume per slice


def swath(axis: int, centroids, values, nn, volume, samples, width: float | None = None,
          n_slices: int = 20) -> Swath:
    """Slices across ``axis`` (0 X, 1 Y, 2 Z), ``width`` metres wide (default: the extent
    in ``n_slices``)."""
    ok = np.isfinite(values)
    c = centroids[ok, axis]
    if not len(c):
        z = np.zeros(0)
        return Swath(axis, z, z, z, z, z, z)
    lo, hi = float(c.min()), float(c.max())
    if width is None or width <= 0:
        width = max((hi - lo) / n_slices, 1e-6)
    edges = np.arange(lo - width / 2, hi + width, width)
    nb = len(edges) - 1
    b = np.clip(np.searchsorted(edges, c, side="right") - 1, 0, nb - 1)
    w = volume[ok]
    v = values[ok]
    vn = np.where(np.isfinite(nn[ok]), nn[ok], 0.0)
    wn = np.where(np.isfinite(nn[ok]), w, 0.0)
    vol = np.bincount(b, weights=w, minlength=nb)
    with np.errstate(invalid="ignore", divide="ignore"):
        est = np.bincount(b, weights=w * v, minlength=nb) / vol
        nnm = np.bincount(b, weights=wn * vn, minlength=nb) / np.bincount(b, weights=wn, minlength=nb)
    s = samples.xyz[:, axis]
    inr = (s >= edges[0]) & (s < edges[-1])
    sb = np.clip(np.searchsorted(edges, s[inr], side="right") - 1, 0, nb - 1)
    sl = samples.length[inr]
    sw = np.bincount(sb, weights=sl, minlength=nb)
    with np.errstate(invalid="ignore", divide="ignore"):
        sm = np.bincount(sb, weights=sl * samples.value[inr], minlength=nb) / sw
    ns = np.bincount(sb, minlength=nb)
    keep = vol > 0
    centre = (edges[:-1] + edges[1:]) / 2
    return Swath(axis, centre[keep], est[keep], nnm[keep], np.where(ns[keep] > 0, sm[keep], np.nan),
                 ns[keep], vol[keep])


def level(value: float, good: float, warn: float) -> str:
    """'ok' / 'warn' / 'bad' for an absolute deviation."""
    a = abs(value)
    if not np.isfinite(a):
        return "warn"
    return "ok" if a <= good else ("warn" if a <= warn else "bad")


def findings(cv: CrossValidation | None, g: GlobalCheck | None, swaths=()) -> list[tuple[str, str]]:
    """(level, plain sentence) per check, for the validation page."""
    out = []
    smooth_ratio = (g.cv_est / g.cv_nn if g is not None and np.isfinite(g.cv_est) and np.isfinite(g.cv_nn)
                    and g.cv_nn > 0 else None)
    slope = cv.stats.get("slope") if cv is not None and cv.stats.get("n", 0) >= 3 else None
    # erratic grades: strong smoothing AND a low slope have one cause - the samples hardly predict
    # each other at this spacing - and opposite search advice would not help
    erratic = (smooth_ratio is not None and smooth_ratio < 0.5 and slope is not None and np.isfinite(slope)
               and slope < GOOD_SLOPE[0])
    if g is not None and np.isfinite(g.bias_pct):
        out.append((level(g.bias_pct, GOOD_BIAS, 2 * GOOD_BIAS),
                    tr("Global mean {est:.4g} against {nn:.4g} for the nearest neighbour model: {b:+.1f} %.",
                       est=g.mean_est, nn=g.mean_nn, b=g.bias_pct)))
        if smooth_ratio is not None and not erratic:
            lv = "ok" if smooth_ratio >= 0.5 else ("warn" if smooth_ratio >= 0.3 else "bad")
            out.append((lv, tr("Spread of the blocks (CV {a:.2f}) is {p:.0f} % of the nearest neighbour model's "
                               "(CV {b:.2f}): {what}.", a=g.cv_est, b=g.cv_nn, p=100 * smooth_ratio,
                               what=tr("normal smoothing") if lv == "ok" else tr("strong smoothing - fewer "
                                                                                    "samples or a shorter search "
                                                                                    "may help"))))
    if erratic:
        out.append(("bad", tr("The grade is erratic at this drill spacing: the samples hardly predict each other "
                              "(cross-validation correlation {c:.2f}, slope {s:.2f}) and the blocks are strongly "
                              "smoothed (CV {a:.2f} against {b:.2f}). Changing the search will not fix this: check "
                              "the domain (ore solid) and the top cut; a reliable local estimate needs closer "
                              "drilling. The global mean can still be used.", c=cv.stats.get("corr", 0.0), s=slope,
                              a=g.cv_est, b=g.cv_nn)))
    if cv is not None and cv.stats.get("n", 0) >= 3:
        s = cv.stats
        lv = level(s["bias_pct"], GOOD_BIAS, 2 * GOOD_BIAS)
        out.append((lv, tr("Cross-validation of {n} samples: mean error {me:+.4g} ({b:+.1f} %), RMSE {rmse:.4g}, "
                           "correlation {c:.2f}.", n=s["n"], me=s["me"], b=s["bias_pct"], rmse=s["rmse"], c=s["corr"])))
        sl = s["slope"]
        if np.isfinite(sl) and not erratic:
            lv = "ok" if GOOD_SLOPE[0] <= sl <= GOOD_SLOPE[1] else (
                "warn" if 0.6 <= sl <= 1.4 else "bad")
            why = (tr("little conditional bias") if lv == "ok" else
                   tr("estimates too spread out (conditional bias): use more samples or a longer search")
                   if sl < 1 else tr("estimates too smooth: use fewer samples or a shorter search"))
            out.append((lv, tr("Slope of regression (true on estimate) {s:.2f}: {why}.", s=sl, why=why)))
        if s["cover"] < 0.95:
            out.append(("warn", tr("{p:.0f} % of the samples could not be estimated with this search.",
                                   p=100 * (1 - s["cover"]))))
    for sw in swaths:
        m = np.isfinite(sw.est) & np.isfinite(sw.samples) & (sw.n_samples >= 5)
        if m.sum() >= 3:
            rel = np.abs(sw.est[m] - sw.samples[m]) / np.maximum(np.abs(sw.samples[m]), 1e-12)
            worst = float(np.median(rel)) * 100
            corr = float(np.corrcoef(sw.est[m], sw.samples[m])[0, 1]) if np.std(sw.est[m]) > 0 else 0.0
            lv = "ok" if corr >= 0.7 else ("warn" if corr >= 0.4 else "bad")
            out.append((lv, tr("Swath along {axis}: slice means of the estimate against the samples' - correlation "
                               "{c:.2f}, median difference {d:.0f} %.", axis="XYZ"[sw.axis], c=corr, d=worst)))
    return out
