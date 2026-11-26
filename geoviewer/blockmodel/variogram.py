"""Semi-variograms: experimental variograms of the samples, variogram models and their
automatic fitting, and suggested lag, model and anisotropy for a set of samples.

Model: a nugget plus one or two structures (spherical, exponential or gaussian, each with
a sill and a practical range), all sharing one anisotropy ellipsoid; the range is along the
major axis, the other axes have range x ratio1 / ratio2.

    spherical    g(h) = c (1.5 h/a - 0.5 (h/a)^3)  for h < a, else c
    exponential  g(h) = c (1 - exp(-3 h / a))
    gaussian     g(h) = c (1 - exp(-3 (h / a)^2))

Fitting is weighted least squares (weights = pairs / lag^2, so the well-informed short lags
that set the nugget count most), with the sills kept >= 0. The range is searched on a fine
grid and the sills solved exactly for each range; the model type with the smallest error
wins (spherical, the usual choice in mining, when it is within 5 % of the best).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

import numpy as np

from ..i18n import N_
from .search import Anisotropy

MODEL_TYPES = {"spherical": N_("Spherical"), "exponential": N_("Exponential"), "gaussian": N_("Gaussian")}
MAX_SAMPLES = 2500            # experimental variograms use a random subset beyond this


def shape(kind: str, h: np.ndarray, a: float) -> np.ndarray:
    """Unit-sill structure value at (major-equivalent) distance h for practical range a."""
    r = np.asarray(h, dtype=np.float64) * (1.0 / max(a, 1e-9))
    if kind == "spherical":
        r = np.minimum(r, 1.0)
        out = r * r
        out *= -0.5
        out += 1.5
        out *= r
        return out
    if kind == "exponential":
        r *= -3.0
        np.exp(r, out=r)
        return np.subtract(1.0, r, out=r)
    if kind == "gaussian":
        r *= r
        r *= -3.0
        np.exp(r, out=r)
        return np.subtract(1.0, r, out=r)
    raise ValueError(kind)


@dataclass
class Structure:
    kind: str = "spherical"
    sill: float = 1.0
    range: float = 100.0


@dataclass
class VariogramModel:
    nugget: float = 0.0
    structures: list[Structure] = field(default_factory=lambda: [Structure()])
    anisotropy: Anisotropy = field(default_factory=Anisotropy)

    @property
    def sill(self) -> float:
        return self.nugget + sum(s.sill for s in self.structures)

    @property
    def range(self) -> float:
        return max((s.range for s in self.structures), default=0.0)

    def gamma(self, h) -> np.ndarray:
        """Variogram at major-equivalent distances h (0 at h = 0)."""
        h = np.asarray(h, dtype=np.float64)
        g = (h > 1e-12).astype(np.float64)
        g *= self.nugget
        for s in self.structures:
            t = shape(s.kind, h, s.range)
            t *= s.sill
            g += t
        return g

    def cov(self, h) -> np.ndarray:
        """Covariance C(h) = sill - gamma(h)."""
        g = self.gamma(h)
        return np.subtract(self.sill, g, out=g)

    def to_dict(self) -> dict:
        return {"nugget": self.nugget, "structures": [vars(s).copy() for s in self.structures],
                "anisotropy": self.anisotropy.to_dict()}

    @classmethod
    def from_dict(cls, d: dict) -> "VariogramModel":
        return cls(float(d.get("nugget", 0)), [Structure(**s) for s in d.get("structures", [])],
                   Anisotropy(**d.get("anisotropy", {})))


@dataclass
class Experimental:
    h: np.ndarray          # mean pair distance per lag
    gamma: np.ndarray
    pairs: np.ndarray
    lag: float
    label: str = ""

    def usable(self, min_pairs: int = 30) -> np.ndarray:
        return self.pairs >= min_pairs


def _subset(xyz, v, max_n, seed=0):
    if len(v) <= max_n:
        return xyz, v
    idx = np.random.default_rng(seed).choice(len(v), max_n, replace=False)
    return xyz[idx], v[idx]


class Pairs:
    """Every sample pair (of a random subset beyond ``max_n``): offset, distance and half
    squared difference - computed once, then binned for any direction."""

    def __init__(self, xyz, v, max_n: int = MAX_SAMPLES):
        xyz, v = _subset(np.asarray(xyz, dtype=np.float64), np.asarray(v, dtype=np.float64), max_n)
        i, j = np.triu_indices(len(v), k=1)
        self.d = (xyz[j] - xyz[i]).astype(np.float32)
        self.dist = np.sqrt(np.einsum("ij,ij->i", self.d, self.d, dtype=np.float64))
        self.g = (0.5 * (v[j] - v[i]) ** 2).astype(np.float64)
        keep = self.dist > 1e-9
        self.d, self.dist, self.g = self.d[keep], self.dist[keep], self.g[keep]

    def bin(self, edges, direction=None, tol_deg: float = 22.5, aniso: Anisotropy | None = None,
            label: str = "") -> "Experimental":
        dist = self.dist
        m = np.ones(len(dist), dtype=bool)
        if direction is not None:
            u = np.asarray(direction, dtype=np.float64)
            m = np.abs(self.d @ u) / np.maximum(dist, 1e-12) >= np.cos(np.radians(tol_deg))
        elif aniso is not None and not aniso.isotropic:
            t = self.d.astype(np.float64) @ aniso.matrix().T
            dist = np.sqrt(np.einsum("ij,ij->i", t, t))
        edges = np.asarray(edges, dtype=np.float64)
        b = np.searchsorted(edges, dist[m], side="right") - 1
        nb = len(edges) - 1
        ok = (b >= 0) & (b < nb)
        b = b[ok]
        cnt = np.bincount(b, minlength=nb)[:nb]
        gs = np.bincount(b, weights=self.g[m][ok], minlength=nb)[:nb]
        hs = np.bincount(b, weights=dist[m][ok], minlength=nb)[:nb]
        with np.errstate(invalid="ignore", divide="ignore"):
            g = gs / cnt
            h = np.where(cnt > 0, hs / np.maximum(cnt, 1), (edges[:-1] + edges[1:]) / 2)
        lag = float(np.median(np.diff(edges))) if nb else 0.0
        return Experimental(h, g, cnt.astype(np.int64), lag, label)


def lag_edges(lag: float, nlags: int, fine: float | None = None) -> np.ndarray:
    """Bin edges: lag k covers ((k - 0.5) lag, (k + 0.5) lag]. With ``fine`` (e.g. the
    composite length, shorter than the lag) the first half lag is split into bins of that
    width, so the short-range (downhole) pairs that set the nugget are seen too."""
    edges = list((np.arange(nlags) + 0.5) * lag)
    if fine and fine < lag / 2:
        short = list(np.arange(fine * 0.5, lag * 0.5 - 1e-9, fine))
        edges = short + edges
    else:
        edges = [0.0] + edges[1:] if lag else edges
    return np.asarray(edges + [(nlags + 0.5) * lag])


def experimental(xyz, v, lag: float, nlags: int, direction=None, tol_deg: float = 22.5,
                 aniso: Anisotropy | None = None, label: str = "", max_n: int = MAX_SAMPLES,
                 fine: float | None = None) -> Experimental:
    """Experimental semi-variogram. ``direction``: a unit vector for a directional variogram
    (pairs within ``tol_deg`` of it), None for omnidirectional. ``aniso``: distances measured
    in that ellipsoid's major-equivalent units (omnidirectional only)."""
    return Pairs(xyz, v, max_n).bin(lag_edges(lag, nlags, fine), direction, tol_deg, aniso, label)


MIN_RATIO = 0.2               # suggested anisotropy ratios stay at or above this


def hole_spacing(xyz, hole=None) -> float:
    """Typical distance from a sample to the nearest sample of another drillhole (median;
    to the nearest other point when there are no holes) - the drill spacing."""
    xyz = np.asarray(xyz, dtype=np.float64)
    sub_idx = np.arange(len(xyz))
    if len(xyz) > 1500:
        sub_idx = np.random.default_rng(0).choice(len(xyz), 1500, replace=False)
    P = xyz[sub_idx]
    d = np.sqrt(((P[:, None, :] - P[None, :, :]) ** 2).sum(-1))
    np.fill_diagonal(d, np.inf)
    if hole is not None and (np.asarray(hole) >= 0).any():
        hh = np.asarray(hole)[sub_idx]
        d[hh[:, None] == hh[None, :]] = np.inf
    nn = d.min(axis=1)
    nn = nn[np.isfinite(nn)]
    return float(np.median(nn)) if len(nn) else 10.0


def suggest_lag(xyz, hole=None, composite: float | None = None) -> tuple[float, int]:
    """(lag, number of lags): the lag is about the typical spacing between neighbouring
    holes (samples of other holes) - or between points - and the lags reach half the
    extent of the data."""
    xyz = np.asarray(xyz, dtype=np.float64)
    spacing = hole_spacing(xyz, hole)
    if composite:
        spacing = max(spacing, float(composite))
    lag = nice_number(max(spacing, 1e-3))
    ext = float(np.linalg.norm(xyz.max(axis=0) - xyz.min(axis=0)))
    nlags = int(np.clip(round(ext / 2 / lag), 6, 30))
    if ext / 2 / lag > 30:
        lag = nice_number(ext / 2 / 30)
        nlags = int(np.clip(round(ext / 2 / lag), 6, 30))
    return lag, nlags


def nice_number(x: float) -> float:
    """1, 2, 2.5, 5 x 10^k at or just above x."""
    if x <= 0:
        return 1.0
    e = np.floor(np.log10(x))
    for m in (1, 2, 2.5, 5, 10):
        if m * 10 ** e >= x * 0.999:
            return float(m * 10 ** e)
    return float(10 ** (e + 1))


def _nnls_small(X: np.ndarray, y: np.ndarray, w: np.ndarray) -> tuple[np.ndarray, float]:
    """Non-negative weighted least squares for a few unknowns (all active sets tried)."""
    sw = np.sqrt(w)
    A = X * sw[:, None]
    b = y * sw
    k = X.shape[1]
    best, best_err = np.zeros(k), float(np.sum(b * b))
    for r in range(1, k + 1):
        for cols in combinations(range(k), r):
            c, *_ = np.linalg.lstsq(A[:, cols], b, rcond=None)
            if np.any(c < 0):
                continue
            coef = np.zeros(k)
            coef[list(cols)] = c
            err = float(np.sum((A @ coef - b) ** 2))
            if err < best_err - 1e-15:
                best, best_err = coef, err
    return best, best_err


def _weights(e: Experimental) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    m = e.usable() & np.isfinite(e.gamma)
    if m.sum() < 3:
        m = (e.pairs >= 5) & np.isfinite(e.gamma)
    h, g, n = e.h[m], e.gamma[m], e.pairs[m].astype(np.float64)
    w = n / np.maximum(h, 1e-9) ** 2
    return h, g, w / w.sum() if len(w) else w


def fit(e: Experimental, kinds=tuple(MODEL_TYPES), nested: bool | None = None,
        anisotropy: Anisotropy | None = None) -> tuple[VariogramModel, float]:
    """Best model for an experimental variogram: (model, weighted error). ``nested``: True
    two structures, False one, None (default) two only when clearly better."""
    h, g, w = _weights(e)
    if len(h) < 2:
        var = float(np.nanmax(e.gamma)) if np.isfinite(e.gamma).any() else 1.0
        return VariogramModel(0.0, [Structure("spherical", var, float(e.h.max()) if len(e.h) else 1.0)],
                              anisotropy or Anisotropy()), float("inf")
    hmax = float(h.max())
    ranges = np.geomspace(max(h.min() * 0.5, 1e-6), hmax, 80)   # no longer than the lags seen
    results = []
    for kind in kinds:
        best = None
        for a in ranges:
            X = np.column_stack([np.ones_like(h), shape(kind, h, a)])
            c, err = _nnls_small(X, g, w)
            if best is None or err < best[0]:
                best = (err, a, c)
        # refine around the best range
        err0, a0, c0 = best
        for a in np.linspace(a0 * 0.85, min(a0 * 1.15, hmax), 31):
            X = np.column_stack([np.ones_like(h), shape(kind, h, a)])
            c, err = _nnls_small(X, g, w)
            if err < best[0]:
                best = (err, a, c)
        err, a, c = best
        results.append((err, kind, VariogramModel(float(c[0]), [Structure(kind, float(c[1]), float(a))])))
    results.sort(key=lambda r: r[0])
    best_err = results[0][0]
    pick = results[0]
    for r in results:
        if r[1] == "spherical" and r[0] <= best_err * 1.05 + 1e-15:
            pick = r
            break
    err, kind, model = pick
    if nested is not False and len(h) >= 6:
        cand = np.geomspace(max(h.min() * 0.5, 1e-6), hmax, 25)
        best2 = None
        for a1, a2 in combinations(cand, 2):
            X = np.column_stack([np.ones_like(h), shape(kind, h, a1), shape(kind, h, a2)])
            c, e2 = _nnls_small(X, g, w)
            if best2 is None or e2 < best2[0]:
                best2 = (e2, a1, a2, c)
        if best2 is not None:
            e2, a1, a2, c = best2
            total = c.sum()
            clearly = e2 < err * 0.8 and total > 0 and min(c[1], c[2]) > 0.05 * total
            if nested or clearly:
                model = VariogramModel(float(c[0]), [Structure(kind, float(c[1]), float(a1)),
                                                     Structure(kind, float(c[2]), float(a2))])
                err = e2
    model.structures = [s for s in model.structures if s.sill > 0] or [Structure(kind, max(model.nugget, 1e-9),
                                                                                  float(hmax))]
    model.anisotropy = anisotropy or Anisotropy()
    return model, float(err)


def _range_scale(e: Experimental, model: VariogramModel) -> float | None:
    """How much longer (> 1) or shorter the ranges must be to fit ``e``, the nugget and sills
    kept; None when ``e`` has too few pairs."""
    m = (e.pairs >= 30) & np.isfinite(e.gamma)
    if m.sum() < 3:
        return None
    h, g, n = e.h[m], e.gamma[m], e.pairs[m]
    w = n / np.maximum(h, 1e-9) ** 2
    best = None
    for k in np.geomspace(0.1, 6.0, 120):
        mm = VariogramModel(model.nugget, [Structure(s.kind, s.sill, s.range * k) for s in model.structures])
        err = float(np.sum(w * (mm.gamma(h) - g) ** 2))
        if best is None or err < best[0]:
            best = (err, k)
    return best[1]


@dataclass
class Suggestion:
    model: VariogramModel
    lag: float
    nlags: int
    omni: Experimental
    directional: dict            # label -> Experimental (along the major, semi-major and minor axes)
    notes: list[str]             # plain sentences: what was found


def direction_vector(bearing: float, plunge: float = 0.0) -> np.ndarray:
    b, p = np.radians([bearing, plunge])
    return np.array([np.sin(b) * np.cos(p), np.cos(b) * np.cos(p), -np.sin(p)])


def suggest_model(xyz, v, hole=None, composite: float | None = None, lag: float | None = None,
                  nlags: int | None = None) -> Suggestion:
    """Experimental variograms of the samples and the model fitted to them, with anisotropy
    from directional variograms (horizontal bearings every 22.5°, and vertical).
    ``composite``: the sample length; lags shorter than half a lag are binned at that width
    (see lag_edges)."""
    from ..i18n import tr

    xyz = np.asarray(xyz, dtype=np.float64)
    v = np.asarray(v, dtype=np.float64)
    if lag is None or nlags is None:
        l2, n2 = suggest_lag(xyz, hole, composite)
        lag, nlags = lag or l2, nlags or n2
    pairs = Pairs(xyz, v)
    edges = lag_edges(lag, nlags, composite)
    omni = pairs.bin(edges, label=tr("Omnidirectional"))
    model, _err = fit(omni)
    notes = []
    var = float(np.var(v))
    mean = float(np.mean(v))
    if mean > 0 and np.sqrt(var) / mean > 2.0:
        notes.append(tr("The values are very skewed (CV {cv:.1f}): the variogram is erratic. A top cut (tab 1, "
                        "Suggest) usually gives a much clearer variogram.", cv=np.sqrt(var) / mean))
    if var > 0:
        notes.append(tr("Nugget is {p:.0f} % of the sill (sample variance {var:.4g}).",
                        p=100 * model.nugget / max(model.sill, 1e-12), var=var))
    # directional ranges
    scales = {}
    for b in np.arange(0, 180, 22.5):
        e = pairs.bin(edges, direction=direction_vector(b), tol_deg=22.5)
        k = _range_scale(e, model)
        if k is not None:
            scales[float(b)] = (k, e)
    ev = pairs.bin(edges, direction=np.array([0.0, 0.0, 1.0]), tol_deg=22.5)
    kv = _range_scale(ev, model)
    aniso = Anisotropy()
    directional = {}
    if len(scales) >= 4:
        bmaj = max(scales, key=lambda b: scales[b][0])
        kmaj, emaj = scales[bmaj]
        bsemi = (bmaj + 90.0) % 180.0
        ksemi = scales.get(bsemi, (None, None))[0]
        if ksemi is not None and kmaj > 0:
            r1 = float(np.clip(ksemi / kmaj, MIN_RATIO, 1.0))
            if r1 < 0.85:
                aniso.bearing = bmaj
                aniso.ratio1 = round(r1, 2)
                cap = 2.0 * float(np.nanmax(omni.h))          # not beyond the extent of the data
                for s in model.structures:
                    s.range = min(s.range * kmaj, cap)
                notes.append(tr("Longest continuity along bearing {b:.1f}°; the range across it is {p:.0f} % of that.",
                                b=bmaj, p=100 * r1))
                directional[tr("Major {b:.1f}°", b=bmaj)] = emaj
                directional[tr("Semi-major {b:.1f}°", b=bsemi)] = scales[bsemi][1]
            else:
                notes.append(tr("No clear horizontal direction of continuity: horizontally isotropic."))
    else:
        notes.append(tr("Too few pairs for directional variograms: isotropic model."))
    if kv is not None:
        kmaj = 1.0
        if aniso.ratio1 < 1:
            kmaj = max(scales[aniso.bearing][0], 1e-9)
        r2 = float(np.clip(kv / kmaj, MIN_RATIO, 1.0))
        if r2 < 0.85:
            aniso.ratio2 = round(r2, 2)
            if aniso.ratio1 > aniso.ratio2 or aniso.ratio1 == 1.0:
                notes.append(tr("Vertical range is {p:.0f} % of the major range.", p=100 * r2))
            directional[tr("Vertical")] = ev
    aniso.ratio2 = min(aniso.ratio2, 1.0)
    if aniso.ratio2 > aniso.ratio1:            # minor must not be longer than semi-major
        aniso.ratio2 = aniso.ratio1
    model.anisotropy = aniso
    notes.append(tr("Model: nugget {c0:.4g} + {parts}.", c0=model.nugget, parts=" + ".join(
        tr("{kind} (sill {c:.4g}, range {a:.1f} m)", kind=tr(MODEL_TYPES[s.kind]), c=s.sill, a=s.range)
        for s in model.structures)))
    return Suggestion(model, lag, nlags, omni, directional, notes)
