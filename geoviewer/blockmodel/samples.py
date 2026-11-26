"""Estimation samples: points with a value, from drillhole assays or a Surpac point string file.

Drillholes
    The assay intervals of one field are composited down each hole into equal lengths
    (``composite`` metres from the collar), each composite the length-weighted mean of the
    assayed parts it covers. A composite covering less than ``min_cover`` of its length
    (e.g. the end of a hole) is dropped. Without compositing every interval is a sample.
    A sample sits at its middle, on the desurveyed hole path.

String file
    Every point of the file is a sample; its value is read from a description field
    (D1, D2, ...: the comma separated text after Z).

Missing values: empty / not a number, the Surpac null (-99) and, when ``negative_missing``,
any value below 0 (assay files often use -1 for "not sampled").
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..i18n import tr


@dataclass
class Samples:
    xyz: np.ndarray              # (N, 3) world coordinates
    value: np.ndarray            # (N,) float64
    hole: np.ndarray             # (N,) int: drillhole number (-1 for string points)
    length: np.ndarray           # (N,) sample length (1 for points)
    source: str = ""
    holes: list[str] = field(default_factory=list)   # hole number -> hole id

    @property
    def n(self) -> int:
        return len(self.value)

    def subset(self, mask) -> "Samples":
        return Samples(self.xyz[mask], self.value[mask], self.hole[mask], self.length[mask], self.source, self.holes)


def _missing(v: np.ndarray, null_value: float, negative_missing: bool) -> np.ndarray:
    bad = ~np.isfinite(v) | (np.abs(v - null_value) < 1e-9)
    if negative_missing:
        bad |= v < 0
    return bad


def drillhole_fields(db) -> dict[str, list[str]]:
    """Numeric fields of each interval table: {table: [field, ...]}."""
    out = {}
    for table, df in db.intervals.items():
        cols = [c for c in df.columns if c not in ("hole_id", "depth_from", "depth_to")]
        num = []
        for c in cols:
            s = df[c]
            try:
                conv = s.astype(float)
            except (TypeError, ValueError):
                import pandas as pd

                conv = pd.to_numeric(s, errors="coerce")
            if np.isfinite(conv.to_numpy(dtype=float)).sum() >= max(1, 0.5 * s.notna().sum()):
                num.append(c)
        if num:
            out[table] = num
    return out


def drillhole_samples(db, table: str, field_name: str, composite: float | None = 2.0, min_cover: float = 0.5,
                      null_value: float = -99.0, negative_missing: bool = True) -> Samples:
    import pandas as pd

    df = db.intervals[table]
    vals = pd.to_numeric(df[field_name], errors="coerce").to_numpy(dtype=np.float64)
    frm = df["depth_from"].to_numpy(dtype=np.float64)
    to = df["depth_to"].to_numpy(dtype=np.float64)
    good = ~_missing(vals, null_value, negative_missing) & np.isfinite(frm) & np.isfinite(to) & (to > frm)
    known = set(db.hole_ids)
    xyz, value, hole, length, names = [], [], [], [], []
    for hid, idx in df.groupby("hole_id", sort=True).indices.items():
        if hid not in known:
            continue
        idx = idx[good[idx]]
        if not len(idx):
            continue
        a, b, v = frm[idx], to[idx], vals[idx]
        if composite and composite > 0:
            n = int(np.ceil(b.max() / composite - 1e-9))
            c0 = np.arange(n) * composite
            c1 = c0 + composite
            ov = np.clip(np.minimum(b[None, :], c1[:, None]) - np.maximum(a[None, :], c0[:, None]), 0, None)  # (C, I)
            cover = ov.sum(axis=1)
            keep = cover >= min_cover * composite - 1e-9
            if not keep.any():
                continue
            ov = ov[keep]
            cover = cover[keep]
            cv = ov @ v / cover
            mid = ((np.minimum(b[None, :], c1[keep, None]) + np.maximum(a[None, :], c0[keep, None])) / 2 * ov).sum(1) / cover
            depth, sval, slen = mid, cv, cover
        else:
            depth, sval, slen = (a + b) / 2, v, b - a
        try:
            pos = db.trace(hid).positions_at(depth)
        except KeyError:
            continue
        k = len(names)
        names.append(str(hid))
        xyz.append(pos)
        value.append(sval)
        hole.append(np.full(len(sval), k))
        length.append(slen)
    if not names:
        raise ValueError(tr("No {field} values in {table}", field=field_name, table=table))
    what = f"{table}.{field_name}" + (f", {composite:g} m composites" if composite else "")
    return Samples(np.vstack(xyz), np.concatenate(value), np.concatenate(hole), np.concatenate(length),
                   what, names)


def string_fields(sf) -> int:
    """How many description fields (D1, D2, ...) the points of a string file have."""
    n = 0
    for d in sf.records_desc:
        if d:
            n = max(n, d.count(",") + 1)
    return n


def string_samples(sf, d_field: int = 1, null_value: float = -99.0, negative_missing: bool = True) -> Samples:
    """Samples from the points of a Surpac string file, the value in field D``d_field``."""
    m = sf.records_string != 0
    pts = sf.records_xyz[m]
    descs = [d for d, keep in zip(sf.records_desc, m) if keep]
    v = np.full(len(descs), np.nan)
    for i, d in enumerate(descs):
        parts = d.split(",") if d else []
        if len(parts) >= d_field:
            try:
                v[i] = float(parts[d_field - 1])
            except ValueError:
                pass
    ok = ~_missing(v, null_value, negative_missing) & np.all(np.isfinite(pts), axis=1)
    if not ok.any():
        raise ValueError(tr("No numbers in field D{n} of {name}", n=d_field, name=sf.path.name))
    n = int(ok.sum())
    return Samples(pts[ok], v[ok], np.full(n, -1), np.ones(n), f"{sf.path.name} D{d_field}", [])


def statistics(v: np.ndarray, weights: np.ndarray | None = None) -> dict:
    """Count, mean, standard deviation, CV and percentiles of sample values."""
    v = np.asarray(v, dtype=np.float64)
    if not len(v):
        return {"n": 0}
    w = np.ones_like(v) if weights is None else np.asarray(weights, dtype=np.float64)
    mean = float(np.average(v, weights=w))
    sd = float(np.sqrt(np.average((v - mean) ** 2, weights=w)))
    q = np.percentile(v, [50, 90, 95, 99])
    return {"n": int(len(v)), "mean": mean, "sd": sd, "cv": sd / mean if mean else float("nan"),
            "min": float(v.min()), "p50": float(q[0]), "p90": float(q[1]), "p95": float(q[2]),
            "p99": float(q[3]), "max": float(v.max())}


def suggest_top_cut(v: np.ndarray) -> float | None:
    """A top cut (cap) for skewed grades: the 99th percentile when the CV is above 1.2 and
    the highest values stand well above it, else None (no cut needed)."""
    s = statistics(v)
    if s.get("n", 0) < 30 or not np.isfinite(s["cv"]) or s["cv"] <= 1.2:
        return None
    if s["max"] <= 1.5 * s["p99"]:
        return None
    return float(s["p99"])
