"""Grade estimation of block model attributes from samples.

Methods
    nn   nearest neighbour (in the search ellipsoid's distance)
    idw  inverse distance weighting, weight 1 / d^power (anisotropic distance)
    ok   ordinary kriging (weights sum to 1; the local mean is unknown)
    sk   simple kriging (a known global mean, by default the sample mean)

Kriging is block kriging: the block is discretised into nx x ny x nz points and the
sample-to-block covariance is their mean (1 x 1 x 1 = point kriging at the centre).

Search: the samples within the search ellipsoid (radius along its major axis), nearest
first, at least ``min_samples`` and at most ``max_samples`` (and at most ``max_per_hole``
from one drillhole). Blocks that do not find enough samples can be tried again in up to
two more passes with the ellipsoid enlarged by ``pass_factor`` each time.

Per block it can store: the estimate, the number of samples, the mean sample distance, the
search pass and, for kriging, the kriging variance, the kriging efficiency and the slope of
regression (the usual checks of estimation quality: a slope near 1 means little
conditional bias).

``suggest_search`` picks min / max samples, the search radius and (IDW) the power by
cross-validation: each test sample is estimated from the others - leaving out its whole
drillhole, which is how blocks between holes are estimated - and the choice with the
smallest error that still estimates nearly every sample wins.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..i18n import N_, tr
from .search import Anisotropy, Neighbours
from .variogram import VariogramModel

METHODS = {"ok": N_("Ordinary kriging"), "sk": N_("Simple kriging"), "idw": N_("Inverse distance (IDW)"),
           "nn": N_("Nearest neighbour")}
OUTPUTS = {"value": N_("Estimate"), "ns": N_("Number of samples"), "dist": N_("Mean distance"),
           "pass": N_("Search pass"), "var": N_("Kriging variance"), "ke": N_("Kriging efficiency"),
           "slope": N_("Slope of regression")}
KRIGING_ONLY = ("var", "ke", "slope")
CHUNK = 3000


class Cancelled(Exception):
    pass


@dataclass
class EstimationParams:
    method: str = "ok"
    power: float = 2.0
    model: VariogramModel = field(default_factory=VariogramModel)
    search: Anisotropy = field(default_factory=Anisotropy)     # the search ellipsoid's shape
    radius: float = 100.0                                      # along the major axis
    min_samples: int = 4
    max_samples: int = 16
    max_per_hole: int = 0                                      # 0 = no limit
    discretisation: tuple[int, int, int] = (3, 3, 2)
    passes: int = 1
    pass_factor: float = 2.0
    sk_mean: float | None = None

    def to_dict(self) -> dict:
        return {"method": self.method, "power": self.power, "model": self.model.to_dict(),
                "search": self.search.to_dict(), "radius": self.radius, "min_samples": self.min_samples,
                "max_samples": self.max_samples, "max_per_hole": self.max_per_hole,
                "discretisation": list(self.discretisation), "passes": self.passes,
                "pass_factor": self.pass_factor, "sk_mean": self.sk_mean}

    @classmethod
    def from_dict(cls, d: dict) -> "EstimationParams":
        p = cls()
        for k, v in d.items():
            if k == "model":
                p.model = VariogramModel.from_dict(v)
            elif k == "search":
                p.search = Anisotropy(**v)
            elif k == "discretisation":
                p.discretisation = tuple(int(x) for x in v)
            elif hasattr(p, k):
                setattr(p, k, v)
        return p


def _unit_offsets(n) -> np.ndarray:
    nx, ny, nz = (max(1, int(v)) for v in n)
    gx = (np.arange(nx) + 0.5) / nx - 0.5
    gy = (np.arange(ny) + 0.5) / ny - 0.5
    gz = (np.arange(nz) + 0.5) / nz - 0.5
    return np.array([(x, y, z) for x in gx for y in gy for z in gz])


def _pair_cov(model: VariogramModel, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Covariance between point sets a (..., n, 3) and b (..., m, 3) (variogram space)."""
    d2 = np.zeros(a.shape[:-1] + (b.shape[-2],))
    for c in range(3):
        t = a[..., :, None, c] - b[..., None, :, c]
        t *= t
        d2 += t
    np.sqrt(d2, out=d2)
    return model.cov(d2)


def _krige(model: VariogramModel, S: np.ndarray, valid: np.ndarray, D: np.ndarray, cbb: np.ndarray,
           ordinary: bool) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Batched kriging. S (B, k, 3) samples and D (B, m, 3) block points, both in variogram
    space; valid (B, k). Returns (weights (B, k), variance (B,), lagrange (B,), sample-block
    covariances (B, k))."""
    B, k, _ = S.shape
    K = _pair_cov(model, S, S)                                   # (B, k, k)
    vf = valid.astype(np.float64)
    K = K * vf[:, :, None] * vf[:, None, :]
    eye = np.eye(k)[None]
    K = K + eye * (1.0 - vf)[:, :, None]                         # unused samples: identity rows
    K = K + eye * (1e-7 * max(model.sill, 1e-12))
    r = _pair_cov(model, S, D).mean(axis=2) * vf                 # (B, k)
    if ordinary:
        A = np.zeros((B, k + 1, k + 1))
        A[:, :k, :k] = K
        A[:, :k, k] = vf
        A[:, k, :k] = vf
        rhs = np.concatenate([r, np.ones((B, 1))], axis=1)
    else:
        A, rhs = K, r
    try:
        sol = np.linalg.solve(A, rhs[..., None])[..., 0]
    except np.linalg.LinAlgError:
        sol = np.stack([np.linalg.lstsq(A[i], rhs[i], rcond=None)[0] for i in range(B)])
    w = sol[:, :k] * vf
    mu = sol[:, k] if ordinary else np.zeros(B)
    var = cbb - np.einsum("ij,ij->i", w, r) - mu
    return w, var, mu, r


def _block_cov(model: VariogramModel, size: np.ndarray, disc) -> float:
    """Mean covariance within a block (variogram space), from its discretisation points."""
    pts = model.anisotropy.transform(_unit_offsets(disc) * size)
    return float(_pair_cov(model, pts, pts).mean())


def estimate(centroids: np.ndarray, sizes: np.ndarray, samples, p: EstimationParams, outputs=("value",),
             progress=None, cancel=None) -> dict[str, np.ndarray]:
    """Estimate at the blocks (centroids (N, 3), sizes (N, 3)). Returns {output: (N,) float64},
    NaN where a block was not estimated."""
    N = len(centroids)
    method = p.method
    kriging = method in ("ok", "sk")
    want = [o for o in outputs if kriging or o not in KRIGING_ONLY]
    out = {o: np.full(N, np.nan) for o in want}
    if N == 0 or samples.n == 0:
        return out
    ref = samples.xyz.mean(axis=0)
    X = samples.xyz - ref
    Z = samples.value.astype(np.float64)
    hole = samples.hole if (samples.hole >= 0).any() else None
    St = p.search.transform(X)
    Sv = p.model.anisotropy.transform(X)
    tree = Neighbours(St, hole)
    k = max(1, int(p.max_samples))
    mean = float(np.mean(Z)) if p.sk_mean is None else float(p.sk_mean)
    offsets = _unit_offsets(p.discretisation if kriging else (1, 1, 1))
    cbb_cache: dict = {}
    todo = np.arange(N)
    n_pass = max(1, min(3, int(p.passes)))
    done_total = 0
    for pas in range(n_pass):
        if not len(todo):
            break
        radius = p.radius * (p.pass_factor ** pas)
        left = []
        for s in range(0, len(todo), CHUNK):
            if cancel is not None and cancel():
                raise Cancelled()
            ids = todo[s:s + CHUNK]
            C = centroids[ids] - ref
            idx, dist = tree.query(p.search.transform(C), k, radius, max_per_group=p.max_per_hole)
            valid = idx >= 0
            ns = valid.sum(axis=1)
            ok = ns >= max(1, int(p.min_samples))
            if not ok.all():
                left.append(ids[~ok])
            if not ok.any():
                continue
            ids, idx, dist, valid, ns = ids[ok], idx[ok], dist[ok], valid[ok], ns[ok]
            safe = np.where(valid, idx, 0)
            zs = Z[safe]
            if method == "nn":
                est = zs[:, 0]
            elif method == "idw":
                d = np.where(valid, dist, np.inf)
                with np.errstate(divide="ignore"):
                    w = np.where(valid, 1.0 / np.maximum(d, 1e-12) ** p.power, 0.0)
                exact = valid & (d < 1e-6)
                hit = exact.any(axis=1)
                w[hit] = exact[hit].astype(np.float64)
                est = np.einsum("ij,ij->i", w, zs) / w.sum(axis=1)
            else:
                S = Sv[safe]
                bsize = sizes[ids].astype(np.float64)
                D = p.model.anisotropy.transform((C[ok][:, None, :] + offsets[None] * bsize[:, None, :]).reshape(-1, 3))
                D = D.reshape(len(ids), len(offsets), 3)
                keys = [tuple(r) for r in np.round(bsize, 6)]
                cbb = np.empty(len(ids))
                for kk in set(keys):
                    if kk not in cbb_cache:
                        cbb_cache[kk] = _block_cov(p.model, np.asarray(kk), p.discretisation)
                for i, kk in enumerate(keys):
                    cbb[i] = cbb_cache[kk]
                w, var, mu, r = _krige(p.model, S, valid, D, cbb, method == "ok")
                if method == "ok":
                    est = np.einsum("ij,ij->i", w, zs)
                else:
                    est = mean + np.einsum("ij,ij->i", w, zs - mean)
                if "var" in out:
                    out["var"][ids] = np.maximum(var, 0.0)
                if "ke" in out:
                    out["ke"][ids] = (cbb - var) / np.maximum(cbb, 1e-12)
                if "slope" in out:
                    cov_zz = np.einsum("ij,ij->i", w, r)
                    var_est = cov_zz - mu
                    out["slope"][ids] = np.where(np.abs(var_est) > 1e-12, cov_zz / var_est, np.nan)
            out["value"][ids] = est
            if "ns" in out:
                out["ns"][ids] = ns
            if "dist" in out:
                out["dist"][ids] = np.where(valid, dist, 0).sum(axis=1) / ns
            if "pass" in out:
                out["pass"][ids] = pas + 1
            done_total += len(ids)
            if progress is not None:
                progress(min(0.999, (s + CHUNK) / max(len(todo), 1) / n_pass + pas / n_pass),
                         tr("Pass {p}: {n:,} of {total:,} blocks estimated", p=pas + 1, n=done_total, total=N))
        todo = np.concatenate(left) if left else np.zeros(0, dtype=np.int64)
    return out


# --- cross-validation and suggestions -------------------------------------------------------
def _cv_estimates(samples, p: EstimationParams, test: np.ndarray, idx: np.ndarray, dist: np.ndarray,
                  radius: float, kmax: int, kmin: int) -> np.ndarray:
    """Estimates at the test samples from (prefixes of) their precomputed neighbours."""
    valid = (idx[:, :kmax] >= 0) & (dist[:, :kmax] <= radius)
    ii = idx[:, :kmax]
    dd = dist[:, :kmax]
    ns = valid.sum(axis=1)
    est = np.full(len(test), np.nan)
    ok = ns >= max(1, kmin)
    if not ok.any():
        return est
    Z = samples.value.astype(np.float64)
    safe = np.where(valid, ii, 0)[ok]
    v = valid[ok]
    zs = Z[safe]
    if p.method == "nn":
        est[ok] = zs[:, 0]
    elif p.method == "idw":
        w = np.where(v, 1.0 / np.maximum(dd[ok], 1e-6) ** p.power, 0.0)
        est[ok] = np.einsum("ij,ij->i", w, zs) / w.sum(axis=1)
    else:
        ref = samples.xyz.mean(axis=0)
        Sv = p.model.anisotropy.transform(samples.xyz - ref)
        S = Sv[safe]
        D = Sv[test[ok]][:, None, :]
        w, _var, _mu, _r = _krige(p.model, S, v, D, np.full(len(D), p.model.sill), p.method == "ok")
        mean = float(np.mean(Z)) if p.sk_mean is None else float(p.sk_mean)
        est[ok] = np.einsum("ij,ij->i", w, zs) if p.method == "ok" else mean + np.einsum("ij,ij->i", w, zs - mean)
    return est


def _score(true: np.ndarray, est: np.ndarray) -> dict:
    ok = np.isfinite(est)
    cover = float(ok.mean()) if len(est) else 0.0
    if ok.sum() < 3:
        return {"rmse": float("inf"), "me": float("nan"), "corr": float("nan"), "cover": cover, "score": float("inf")}
    e = est[ok] - true[ok]
    rmse = float(np.sqrt(np.mean(e * e)))
    corr = float(np.corrcoef(true[ok], est[ok])[0, 1]) if np.std(est[ok]) > 0 else 0.0
    return {"rmse": rmse, "me": float(e.mean()), "corr": corr, "cover": cover,
            "score": rmse * (1.0 + 4.0 * max(0.0, 0.98 - cover))}


def cross_validate(samples, p: EstimationParams, n_test: int = 600, seed: int = 0) -> dict:
    """Leave-one-hole-out (or leave-one-out for points) cross-validation of ``p``."""
    test, idx, dist = _cv_neighbours(samples, p.search, p.radius, p.max_samples, p.max_per_hole, n_test, seed)
    est = _cv_estimates(samples, p, test, idx, dist, p.radius, p.max_samples, p.min_samples)
    return _score(samples.value[test], est)


def _cv_neighbours(samples, search: Anisotropy, radius, kmax, max_per_hole, n_test, seed):
    rng = np.random.default_rng(seed)
    test = np.arange(samples.n) if samples.n <= n_test else np.sort(rng.choice(samples.n, n_test, replace=False))
    ref = samples.xyz.mean(axis=0)
    St = search.transform(samples.xyz - ref)
    holes = samples.hole >= 0
    group = samples.hole.copy() if holes.any() else np.arange(samples.n)
    tree = Neighbours(St, group)
    idx, dist = tree.query(St[test], kmax, radius, exclude=group[test], max_per_group=max_per_hole if holes.any() else 0)
    return test, idx, dist


@dataclass
class SearchSuggestion:
    radius: float
    min_samples: int
    max_samples: int
    max_per_hole: int
    power: float
    score: dict
    tried: int
    notes: list[str]


MAXES = (4, 6, 8, 12, 16, 24, 32)
MINS = (1, 2, 3, 4, 6, 8)


def search_plan(samples, p: EstimationParams) -> dict:
    """What ``suggest_search`` tries: radii (around the variogram range, never shorter than the
    drill spacing - a block between holes must reach more than one hole), samples per hole,
    IDW powers. Each (per hole, power, radius) is one task, so they can run in parallel."""
    from .variogram import hole_spacing

    base_r = p.model.range if p.method in ("ok", "sk") and p.model.range > 0 else p.radius
    spacing = hole_spacing(samples.xyz, samples.hole)
    radii = sorted({round(r, 3) for r in [base_r * f for f in (0.5, 0.75, 1.0, 1.25, 1.5)]
                    + [spacing * f for f in (1.5, 2.0, 3.0, 4.0)] if r >= spacing})
    powers = [1.0, 2.0, 3.0] if p.method == "idw" else [p.power]
    holes = bool((samples.hole >= 0).any())
    per_hole = [0, 3, 4] if holes else [0]
    tasks = [(mph, pw, r) for mph in per_hole for pw in powers for r in radii]
    return {"base_r": base_r, "spacing": spacing, "tasks": tasks, "holes": holes}


def search_task(samples, p: EstimationParams, mph: int, pw: float, r: float, base_r: float,
                n_test: int = 500) -> tuple:
    """The best (min, max samples) for one radius / samples per hole / power: (key, r, kmin,
    kmax, mph, pw, score) or None, and the number of combinations tried."""
    test, idx, dist = _cv_neighbours(samples, p.search, r, max(MAXES), mph, n_test, 0)
    true = samples.value[test]
    q = EstimationParams(**{**p.__dict__, "power": pw})
    best, tried = None, 0
    for kmax in MAXES:
        if mph and kmax < 2 * mph:
            continue
        for kmin in MINS:
            if kmin > kmax:
                continue
            s = _score(true, _cv_estimates(samples, q, test, idx, dist, r, kmax, kmin))
            tried += 1
            # nearly all samples estimated first, then the smallest error; ties: the radius
            # nearest the range, fewer samples, a larger minimum
            key = (0 if s["cover"] >= 0.95 else 1, round(s["score"], 6) if s["cover"] >= 0.95 else -s["cover"],
                   abs(float(np.log(r / base_r))), kmax, -kmin)
            if best is None or key < best[0]:
                best = (key, r, kmin, kmax, mph, pw, s)
    return best, tried


def pick_search(samples, p: EstimationParams, plan: dict, results, n_test: int = 500) -> SearchSuggestion:
    found = [b for b, _t in results if b is not None]
    tried = sum(t for _b, t in results)
    if not found:
        raise ValueError(tr("No search parameters could estimate the samples"))
    _key, r, kmin, kmax, mph, pw, s = min(found, key=lambda b: b[0])
    t = min(samples.n, n_test)
    notes = [tr("Tried {n} combinations on {t} samples, each estimated without its own drillhole.", n=tried, t=t)
             if plan["holes"] else
             tr("Tried {n} combinations on {t} samples, each estimated without itself.", n=tried, t=t),
             tr("Best: error (RMSE) {rmse:.4g}, correlation {corr:.2f}, {cover:.0f} % of samples estimated.",
                rmse=s["rmse"], corr=s["corr"], cover=100 * s["cover"]),
             tr("Drill spacing about {s:.1f} m; search radius {r:.1f} m.", s=plan["spacing"], r=r)]
    if p.method in ("ok", "sk"):
        notes.append(tr("Search radius = {f:.2f} x the variogram range.", f=r / max(plan["base_r"], 1e-9)))
    return SearchSuggestion(float(r), int(kmin), int(kmax), int(mph), float(pw), s, tried, notes)


def suggest_search(samples, p: EstimationParams, n_test: int = 500, progress=None) -> SearchSuggestion:
    """Cross-validate a grid of search parameters for method ``p.method`` (with its variogram
    and search ellipsoid shape) and return the best (in this process; see
    ``parallel.suggest_search`` for the multi-core version)."""
    plan = search_plan(samples, p)
    results = []
    for i, (mph, pw, r) in enumerate(plan["tasks"]):
        results.append(search_task(samples, p, mph, pw, r, plan["base_r"], n_test))
        if progress is not None:
            progress((i + 1) / len(plan["tasks"]))
    return pick_search(samples, p, plan, results, n_test)


def default_params(model: VariogramModel, method: str = "ok") -> EstimationParams:
    """Starting parameters from a variogram model: search ellipsoid = the variogram's, radius
    = its range."""
    return EstimationParams(method=method, model=model, search=Anisotropy(**model.anisotropy.to_dict()),
                            radius=max(model.range, 1e-3))
