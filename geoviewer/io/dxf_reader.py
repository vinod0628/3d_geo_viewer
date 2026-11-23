"""Minimal ASCII DXF reader for geometry: LINE/3DLINE, POLYLINE/VERTEX, LWPOLYLINE, 3DFACE.

Surpac writes old-style R10/R12 DXF (e.g. ``3DLINE`` entities) that modern
libraries skip, so we read the group codes directly. Line pieces whose ends
meet are chained into polylines. DXF layers become string numbers when they
are numeric, otherwise they are numbered in order of appearance.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .surpac_dtm import DtmFile, DtmObject, Trisolation
from .surpac_str import StrHeader, StringFile


def _pairs(path: Path):
    with open(path, "r", encoding="latin-1", errors="replace") as f:
        lines = f.read().splitlines()
    for i in range(0, len(lines) - 1, 2):
        try:
            code = int(lines[i].strip())
        except ValueError:
            continue
        yield code, lines[i + 1].strip()


def _entities(path: Path):
    """Yield (type, [(code, value), ...]) for entities in the ENTITIES section."""
    in_entities = False
    cur_type, cur = None, []
    for code, value in _pairs(path):
        if code == 0:
            if cur_type is not None and in_entities:
                yield cur_type, cur
            cur_type, cur = value.upper(), []
            if value.upper() == "ENDSEC":
                in_entities = False
                cur_type = None
            continue
        if code == 2 and cur_type == "SECTION":
            in_entities = value.upper() == "ENTITIES"
            cur_type = None
            continue
        if cur_type is not None:
            cur.append((code, value))


def _get(groups, code, default=0.0):
    for c, v in groups:
        if c == code:
            return float(v)
    return default


def _layer(groups) -> str:
    for c, v in groups:
        if c == 8:
            return v
    return "0"


def _chain(segments: list[tuple[np.ndarray, np.ndarray]], tol=1e-6) -> list[np.ndarray]:
    """Join consecutive line pieces whose end equals the next start."""
    out: list[list[np.ndarray]] = []
    for a, b in segments:
        if out and np.allclose(out[-1][-1], a, atol=tol):
            out[-1].append(b)
        else:
            out.append([a, b])
    return [np.array(p) for p in out]


def read_dxf(path: str | Path) -> tuple[StringFile | None, DtmFile | None]:
    path = Path(path)
    layer_ids: dict[str, int] = {}

    def layer_no(name: str) -> int:
        if name not in layer_ids:
            try:
                n = int(float(name))
                layer_ids[name] = n if n > 0 else len(layer_ids) + 1
            except ValueError:
                layer_ids[name] = len(layer_ids) + 1
        return layer_ids[name]

    polylines: list[tuple[int, np.ndarray]] = []
    line_pieces: dict[int, list] = {}
    faces: list[np.ndarray] = []

    poly_layer, poly_pts, poly_closed = None, None, False
    for etype, g in _entities(path):
        if etype in ("LINE", "3DLINE"):
            a = np.array([_get(g, 10), _get(g, 20), _get(g, 30)])
            b = np.array([_get(g, 11), _get(g, 21), _get(g, 31)])
            line_pieces.setdefault(layer_no(_layer(g)), []).append((a, b))
        elif etype == "POLYLINE":
            poly_layer, poly_pts = layer_no(_layer(g)), []
            poly_closed = int(_get(g, 70, 0)) & 1 == 1
        elif etype == "VERTEX" and poly_pts is not None:
            poly_pts.append((_get(g, 10), _get(g, 20), _get(g, 30)))
        elif etype == "SEQEND" and poly_pts is not None:
            if len(poly_pts) >= 2:
                p = np.array(poly_pts)
                if poly_closed:
                    p = np.vstack([p, p[:1]])
                polylines.append((poly_layer, p))
            poly_pts = None
        elif etype == "LWPOLYLINE":
            z = _get(g, 38, 0.0)
            xs = [float(v) for c, v in g if c == 10]
            ys = [float(v) for c, v in g if c == 20]
            if len(xs) >= 2:
                p = np.column_stack([xs, ys, np.full(len(xs), z)])
                if int(_get(g, 70, 0)) & 1:
                    p = np.vstack([p, p[:1]])
                polylines.append((layer_no(_layer(g)), p))
        elif etype == "3DFACE":
            q = np.array([[_get(g, 10 + k), _get(g, 20 + k), _get(g, 30 + k)] for k in range(4)])
            faces.append(q[:3])
            if not np.allclose(q[2], q[3]):
                faces.append(q[[0, 2, 3]])

    for lay, pieces in line_pieces.items():
        for p in _chain(pieces):
            polylines.append((lay, p))

    sf = None
    if polylines:
        sns, xyz, starts, ends = [], [], [], []
        for lay, p in polylines:
            starts.append(len(sns))
            sns.extend([lay] * len(p))
            xyz.extend(p.tolist())
            ends.append(len(sns))
            sns.append(0)               # separator record, as in a .str file
            xyz.append([np.nan] * 3)
        sf = StringFile(
            path=path,
            header=StrHeader(name=path.stem),
            records_xyz=np.array(xyz, dtype=np.float64),
            records_string=np.array(sns, dtype=np.int32),
            records_desc=[""] * len(sns),
            seg_string=np.array([lay for lay, _ in polylines], dtype=np.int32),
            seg_start=np.array(starts, dtype=np.int64),
            seg_end=np.array(ends, dtype=np.int64),
        )

    dtm = None
    if faces:
        verts = np.vstack(faces)
        tris = np.arange(len(verts), dtype=np.int64).reshape(-1, 3)
        dtm = DtmFile(
            path=path, str_name="", checksum="", options={}, vertices=verts,
            objects=[DtmObject(1, [Trisolation(1, tris, None, np.arange(1, len(tris) + 1),
                                               {"closed": "no"})])],
        )
    return sf, dtm
