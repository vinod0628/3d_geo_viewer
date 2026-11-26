"""Anisotropy (the search / variogram ellipsoid) and the neighbour search.

Ellipsoid angles follow the usual mining convention:

* bearing - azimuth of the major axis, degrees clockwise from north
* plunge  - how far the major axis dips below horizontal (positive down)
* dip     - tilt of the semi-major axis about the major axis

``ratio1`` = semi-major / major and ``ratio2`` = minor / major. ``Anisotropy.transform``
rotates points onto the ellipsoid's axes and stretches the short axes, so the ellipsoid
becomes a sphere of the major radius: distances there are "major-axis equivalent" and the
same code serves the search and the variogram.

The search buckets the samples on a grid of cells at least one search radius wide, so the
samples within the radius of a block are all in the 27 cells around it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np


@dataclass
class Anisotropy:
    bearing: float = 0.0
    plunge: float = 0.0
    dip: float = 0.0
    ratio1: float = 1.0
    ratio2: float = 1.0

    @property
    def isotropic(self) -> bool:
        return abs(self.ratio1 - 1) < 1e-9 and abs(self.ratio2 - 1) < 1e-9

    def axes(self) -> np.ndarray:
        """(3, 3) unit vectors (rows) of the major, semi-major and minor axes."""
        b, p, d = np.radians([self.bearing, self.plunge, self.dip])
        major = np.array([np.sin(b) * np.cos(p), np.cos(b) * np.cos(p), -np.sin(p)])
        semi0 = np.array([np.cos(b), -np.sin(b), 0.0])
        minor0 = np.cross(semi0, major)
        semi = np.cos(d) * semi0 + np.sin(d) * minor0
        minor = -np.sin(d) * semi0 + np.cos(d) * minor0
        return np.vstack([major, semi, minor])

    def matrix(self) -> np.ndarray:
        """World offsets -> major-equivalent offsets (rotate, then stretch the short axes)."""
        r1 = max(self.ratio1, 1e-6)
        r2 = max(self.ratio2, 1e-6)
        return np.diag([1.0, 1.0 / r1, 1.0 / r2]) @ self.axes()

    def transform(self, pts: np.ndarray, ref: np.ndarray | None = None) -> np.ndarray:
        pts = np.asarray(pts, dtype=np.float64)
        if ref is not None:
            pts = pts - ref
        return pts @ self.matrix().T

    def to_dict(self) -> dict:
        return asdict(self)


def _topk(d2: np.ndarray, ids: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    """The k smallest of each row of d2 (R, C) with their ids (R, C), nearest first."""
    C = d2.shape[1]
    kk = min(k, C)
    if kk < C:
        part = np.argpartition(d2, kk - 1, axis=1)[:, :kk]
    else:
        part = np.broadcast_to(np.arange(C), d2.shape)
    dp = np.take_along_axis(d2, part, axis=1)
    o = np.argsort(dp, axis=1, kind="stable")
    part = np.take_along_axis(part, o, axis=1)
    return np.take_along_axis(ids, part, axis=1), np.take_along_axis(dp, o, axis=1)


def _limit_groups(d2: np.ndarray, g: np.ndarray, m: int) -> np.ndarray:
    """d2 with all but the ``m`` nearest samples of each group (g: same shape, -1 = no group)
    in every row set to inf."""
    order = np.argsort(d2, axis=1, kind="stable")
    g_near = np.take_along_axis(g, order, axis=1)                  # groups, nearest first
    gs = np.argsort(g_near, axis=1, kind="stable")                 # same groups together, by distance
    gsorted = np.take_along_axis(g_near, gs, axis=1)
    newgrp = np.ones(gsorted.shape, dtype=bool)
    newgrp[:, 1:] = gsorted[:, 1:] != gsorted[:, :-1]
    pos = np.broadcast_to(np.arange(g.shape[1]), g.shape)
    start = np.maximum.accumulate(np.where(newgrp, pos, 0), axis=1)
    rank = np.empty(pos.shape, dtype=np.int64)
    np.put_along_axis(rank, gs, pos - start, axis=1)               # rank within the group, by distance
    drop = np.zeros(d2.shape, dtype=bool)
    np.put_along_axis(drop, order, (rank >= m) & (g_near >= 0), axis=1)
    out = d2.copy()
    out[drop] = np.inf
    return out


class Neighbours:
    """Nearest samples within a radius (in transformed coordinates)."""

    def __init__(self, pts_t: np.ndarray, group: np.ndarray | None = None):
        self.p = np.asarray(pts_t, dtype=np.float64)
        self.pp = np.einsum("ij,ij->i", self.p, self.p)
        self.group = np.asarray(group) if group is not None else None

    def query(self, q: np.ndarray, k: int, radius: float, exclude: np.ndarray | None = None,
              max_per_group: int = 0, progress=None) -> tuple[np.ndarray, np.ndarray]:
        """For each query point the ``k`` nearest samples closer than ``radius``.

        ``exclude`` (M,): a group id per query whose samples are skipped (leave-one-hole-out;
        -1 = none). ``max_per_group``: at most that many samples of one group (drillhole).
        Returns (index (M, k), -1 = none; distance (M, k), inf = none), nearest first."""
        q = np.asarray(q, dtype=np.float64)
        M = len(q)
        idx_out = np.full((M, k), -1, dtype=np.int64)
        d_out = np.full((M, k), np.inf)
        if M == 0 or len(self.p) == 0 or k <= 0 or radius <= 0:
            return idx_out, d_out
        lo = np.minimum(self.p.min(axis=0), q.min(axis=0))
        hi = np.maximum(self.p.max(axis=0), q.max(axis=0))
        span = np.maximum(hi - lo, 1e-9)
        qspan = np.maximum(q.max(axis=0) - q.min(axis=0), radius)
        cs = max(radius, float(np.cbrt(np.prod(qspan) / 4000.0)))     # few thousand query cells at most
        dims = (np.floor(span / cs).astype(np.int64) + 3)
        sc = np.floor((self.p - lo) / cs).astype(np.int64) + 1
        skey = (sc[:, 0] * dims[1] + sc[:, 1]) * dims[2] + sc[:, 2]
        sorder = np.argsort(skey, kind="stable")
        skey_sorted = skey[sorder]
        qc = np.floor((q - lo) / cs).astype(np.int64) + 1
        qkey = (qc[:, 0] * dims[1] + qc[:, 1]) * dims[2] + qc[:, 2]
        ukeys, inv = np.unique(qkey, return_inverse=True)
        offs = np.array([(a * dims[1] + b) * dims[2] + c for a in (-1, 0, 1) for b in (-1, 0, 1) for c in (-1, 0, 1)])
        groups_of = np.argsort(inv, kind="stable")
        bounds = np.searchsorted(inv[groups_of], np.arange(len(ukeys) + 1))
        qq = np.einsum("ij,ij->i", q, q)
        slack = radius * radius * (1 + 1e-9) + 1e-9      # the fast distance rounds; decided exactly below
        for u, key in enumerate(ukeys):
            nkeys = key + offs
            a = np.searchsorted(skey_sorted, nkeys, side="left")
            b = np.searchsorted(skey_sorted, nkeys, side="right")
            if not (b > a).any():
                continue
            cand = sorder[np.concatenate([np.arange(x, y) for x, y in zip(a, b) if y > x])]
            P = self.p[cand]
            ppc = self.pp[cand]
            gc = self.group[cand] if self.group is not None else None
            C = len(cand)
            qi_all = groups_of[bounds[u]:bounds[u + 1]]
            step = max(1, 2_000_000 // C)
            for s in range(0, len(qi_all), step):
                qi = qi_all[s:s + step]
                d2 = qq[qi, None] + ppc[None, :] - 2.0 * (q[qi] @ P.T)      # |q - p|^2 by one matrix product
                np.maximum(d2, 0.0, out=d2)
                d2[d2 > slack] = np.inf
                if exclude is not None and gc is not None:
                    d2[gc[None, :] == exclude[qi, None]] = np.inf
                ids2 = np.broadcast_to(cand, d2.shape)
                if max_per_group and gc is not None:
                    pre = min(C, max(16 * k, 256))          # the nearest few hundred are nearly always enough
                    if pre < C:
                        part = np.argpartition(d2, pre - 1, axis=1)[:, :pre]
                        d2p = np.take_along_axis(d2, part, axis=1)
                        idp = cand[part]
                        ids, dd = _topk(_limit_groups(d2p, self.group[idp], max_per_group), idp, k)
                        want = np.minimum(np.isfinite(d2).sum(axis=1), k)
                        short = np.isfinite(dd).sum(axis=1) < want
                        if short.any():                     # a few holes fill the nearest: use all of them
                            rows = np.flatnonzero(short)
                            d2s = _limit_groups(d2[rows], np.broadcast_to(gc, (len(rows), C)), max_per_group)
                            ids[rows], dd[rows] = _topk(d2s, ids2[rows], k)
                    else:
                        ids, dd = _topk(_limit_groups(d2, np.broadcast_to(gc, d2.shape), max_per_group), ids2, k)
                else:
                    ids, dd = _topk(d2, ids2, k)
                # exact distances of the chosen few (the fast form loses digits for close points)
                ok = np.isfinite(dd)
                diff = q[qi][:, None, :] - self.p[np.where(ok, ids, 0)]
                de = np.sqrt(np.einsum("ijk,ijk->ij", diff, diff))
                ok &= de <= radius
                de = np.where(ok, de, np.inf)
                o = np.argsort(de, axis=1, kind="stable")
                ids = np.where(ok, ids, -1)
                kk = ids.shape[1]
                idx_out[qi, :kk] = np.take_along_axis(ids, o, axis=1)
                d_out[qi, :kk] = np.take_along_axis(de, o, axis=1)
            if progress is not None and u % 64 == 0:
                progress(u / len(ukeys))
        return idx_out, d_out
