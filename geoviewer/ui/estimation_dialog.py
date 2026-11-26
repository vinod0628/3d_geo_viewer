"""Grade estimation of a block model attribute, in four steps (tabs):

1. Samples    drillhole assays (composited) or the points of a Surpac string file, with
              missing values, a top cut and an optional domain solid; their statistics.
2. Variogram  experimental variograms (omnidirectional and along the model's axes) and the
              model: nugget plus one or two structures and the anisotropy. "Suggest model"
              fits it automatically.
3. Estimate   method (ordinary / simple kriging, inverse distance, nearest neighbour),
              search ellipsoid, min / max samples ("Suggest search" picks them by
              cross-validation), passes, discretisation, which blocks, extra outputs.
4. Validate   cross-validation, global bias against a nearest neighbour model, smoothing
              and swath plots, with traffic-light findings in plain sentences.

The result is written into the block model in memory; the manager saves it. What was done
(samples, variogram, parameters) is kept with the block model, so the dialog opens with the
same settings next time and a validation can be repeated.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..blockmodel import parallel
from ..blockmodel import samples as bsamp
from ..blockmodel import validate as bval
from ..blockmodel.estimate import KRIGING_ONLY, METHODS, OUTPUTS, Cancelled, EstimationParams
from ..blockmodel.search import Anisotropy
from ..blockmodel.store import add_history
from ..blockmodel.variogram import MODEL_TYPES, Pairs, Structure, VariogramModel, lag_edges, suggest_lag, \
    suggest_model
from ..i18n import tr
from ..scene.layers import DEPLETION_ATTRS, fmt_num
from . import theme
from .ai_widgets import AMBER, BLUE, GREEN, RED, FindingList, _Chart, nice
from .block_manager import _muted, run_with_progress, solid_layers, solid_mesh
from .icons import icon

OUTPUT_SUFFIX = {"ns": "_ns", "dist": "_dist", "pass": "_pass", "var": "_kv", "ke": "_ke", "slope": "_sor"}


def _spin(value, lo, hi, dec=3, step=1.0) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(dec)
    s.setSingleStep(step)
    s.setValue(value)
    s.setMinimumWidth(70)
    return s


def _ispin(value, lo, hi) -> QSpinBox:
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setValue(value)
    return s


def _ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    if not (np.isfinite(lo) and np.isfinite(hi)) or hi <= lo:
        return [lo]
    step = nice((hi - lo) / n)
    start = np.ceil(lo / step) * step
    return list(np.arange(start, hi + step * 0.001, step))


class _XYChart(_Chart):
    """Axes, grid and labels for the charts below."""

    def __init__(self, xlabel="", ylabel="", parent=None, min_h=230):
        super().__init__(parent, min_h)
        self.xlabel, self.ylabel = xlabel, ylabel
        self.xr = (0.0, 1.0)
        self.yr = (0.0, 1.0)

    def X(self, v):
        r = self.plot_rect()
        return r.left() + (v - self.xr[0]) / max(self.xr[1] - self.xr[0], 1e-12) * r.width()

    def Y(self, v):
        r = self.plot_rect()
        return r.bottom() - (v - self.yr[0]) / max(self.yr[1] - self.yr[0], 1e-12) * r.height()

    def axes(self, p: QPainter) -> None:
        r = self.plot_rect()
        p.setPen(QPen(QColor(theme.BORDER), 1))
        for t in _ticks(*self.yr):
            y = self.Y(t)
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            self._text(p, r.left() - 6, y, fmt_num(t, 4), align=Qt.AlignRight | Qt.AlignVCenter, w=54)
            p.setPen(QPen(QColor(theme.BORDER), 1))
        for t in _ticks(*self.xr):
            x = self.X(t)
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            self._text(p, x, r.bottom() + 10, fmt_num(t, 3), align=Qt.AlignHCenter | Qt.AlignVCenter, w=70)
            p.setPen(QPen(QColor(theme.BORDER), 1))
        self._text(p, r.center().x(), r.bottom() + 27, self.xlabel, align=Qt.AlignHCenter | Qt.AlignVCenter, w=400)
        self._text(p, r.left(), r.top() - 8, self.ylabel, w=400)

    def legend(self, p, items) -> None:
        r = self.plot_rect()
        x = r.right() - 170
        y = r.top() + 10
        for color, text, kind in items:
            p.setPen(QPen(QColor(color), 2, Qt.DashLine if kind == "dash" else Qt.SolidLine))
            if kind == "dot":
                p.setBrush(QColor(color))
                p.drawEllipse(QPointF(x + 8, y), 3.5, 3.5)
            else:
                p.drawLine(QPointF(x, y), QPointF(x + 16, y))
            self._text(p, x + 22, y, text, color=theme.TEXT, w=150)
            y += 16


class VariogramChart(_XYChart):
    """Experimental points (labelled with their pair counts) and the model curve."""

    def __init__(self, parent=None):
        super().__init__(tr("Distance (m)"), tr("Semi-variogram γ(h)"), parent, 280)
        self.exp = None
        self.model_h = np.zeros(0)
        self.model_g = np.zeros(0)
        self.sill = None
        self.variance = None

    def set_data(self, exp, model_h, model_g, sill, variance) -> None:
        self.exp, self.model_h, self.model_g, self.sill, self.variance = exp, model_h, model_g, sill, variance
        hs = [float(np.nanmax(exp.h))] if exp is not None and len(exp.h) else []
        # the axis follows the well-informed lags (a lag of a few dozen pairs among thousands is noise)
        well = (exp.pairs >= max(30, 0.005 * float(exp.pairs.max(initial=0)))) if exp is not None else None
        if exp is not None and not well.any():
            well = exp.pairs > 0
        gs = [float(np.nanmax(exp.gamma[well]))] if exp is not None and well.any() else []
        gs += [float(np.nanmax(model_g))] if len(model_g) else []
        gs += [v for v in (sill, variance) if v]
        self.xr = (0.0, nice(max(hs + [float(model_h.max()) if len(model_h) else 1.0]) * 1.02))
        self.yr = (0.0, nice(max(gs + [1e-9]) * 1.1))
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        self._bg(p)
        self.axes(p)
        r = self.plot_rect()
        if self.variance:
            p.setPen(QPen(QColor(theme.MUTED), 1, Qt.DotLine))
            p.drawLine(QPointF(r.left(), self.Y(self.variance)), QPointF(r.right(), self.Y(self.variance)))
        if len(self.model_h) > 1:
            p.setPen(QPen(QColor(AMBER), 2))
            pts = [QPointF(self.X(h), self.Y(g)) for h, g in zip(self.model_h, self.model_g)]
            for a, b in zip(pts[:-1], pts[1:]):
                p.drawLine(a, b)
        e = self.exp
        if e is not None:
            mx = max(int(e.pairs.max(initial=1)), 1)
            p.save()
            p.setClipRect(self.plot_rect().adjusted(-8, -8, 8, 8))
            for h, g, n in zip(e.h, e.gamma, e.pairs):
                if n <= 0 or not np.isfinite(g):
                    continue
                rad = 2.5 + 4.0 * np.sqrt(n / mx)
                color = BLUE if n >= max(30, 0.005 * mx) else theme.MUTED
                p.setPen(QPen(QColor(color), 1))
                p.setBrush(QColor(color))
                p.drawEllipse(QPointF(self.X(h), self.Y(g)), rad, rad)
                self._text(p, self.X(h), self.Y(g) - 12, fmt_num(int(n)), align=Qt.AlignHCenter | Qt.AlignVCenter,
                           size=7, w=60)
            p.restore()
        self.legend(p, [(BLUE, tr("experimental (pairs)"), "dot"), (AMBER, tr("model"), "line"),
                        (theme.MUTED, tr("sample variance"), "dash")])


class ScatterChart(_XYChart):
    def __init__(self, parent=None):
        super().__init__(tr("Estimate"), tr("True value"), parent, 260)
        self.x = np.zeros(0)
        self.y = np.zeros(0)
        self.slope = None

    def set_data(self, est, true, slope=None) -> None:
        m = np.isfinite(est) & np.isfinite(true)
        self.x, self.y, self.slope = est[m], true[m], slope
        hi = float(np.percentile(np.concatenate([self.x, self.y]), 99.5)) if m.any() else 1.0
        lo = min(0.0, float(min(self.x.min(initial=0), self.y.min(initial=0))))
        self.xr = self.yr = (lo, nice(max(hi, 1e-9) * 1.05))
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        self._bg(p)
        self.axes(p)
        r = self.plot_rect()
        p.save()
        p.setClipRect(r)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(61, 155, 255, 110))
        for a, b in zip(self.x, self.y):
            p.drawEllipse(QPointF(self.X(a), self.Y(b)), 2.4, 2.4)
        lo, hi = self.xr
        p.setPen(QPen(QColor(theme.MUTED), 1, Qt.DashLine))
        p.drawLine(QPointF(self.X(lo), self.Y(lo)), QPointF(self.X(hi), self.Y(hi)))
        if self.slope is not None and np.isfinite(self.slope) and len(self.x) > 2:
            mx, my = self.x.mean(), self.y.mean()
            p.setPen(QPen(QColor(AMBER), 2))
            p.drawLine(QPointF(self.X(lo), self.Y(my + self.slope * (lo - mx))),
                       QPointF(self.X(hi), self.Y(my + self.slope * (hi - mx))))
        p.restore()
        self.legend(p, [(theme.MUTED, tr("1 : 1"), "dash"), (AMBER, tr("regression"), "line")])


class SwathChart(_XYChart):
    def __init__(self, parent=None):
        super().__init__("", tr("Mean"), parent, 230)
        self.sw = None

    def set_swath(self, sw) -> None:
        self.sw = sw
        self.xlabel = tr("{axis} (m)", axis="XYZ"[sw.axis]) if sw is not None else ""
        if sw is not None and len(sw.centre):
            vals = np.concatenate([sw.est, sw.nn, sw.samples])
            vals = vals[np.isfinite(vals)]
            half = (sw.centre[1] - sw.centre[0]) / 2 if len(sw.centre) > 1 else 1.0
            self.xr = (float(sw.centre.min() - half), float(sw.centre.max() + half))
            self.yr = (0.0, nice(float(vals.max(initial=1e-9)) * 1.1))
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        self._bg(p)
        if self.sw is None or not len(self.sw.centre):
            return
        self.axes(p)
        sw = self.sw
        for vals, color, dash in ((sw.nn, theme.MUTED, True), (sw.est, AMBER, False)):
            p.setPen(QPen(QColor(color), 2, Qt.DashLine if dash else Qt.SolidLine))
            pts = [QPointF(self.X(c), self.Y(v)) for c, v in zip(sw.centre, vals) if np.isfinite(v)]
            for a, b in zip(pts[:-1], pts[1:]):
                p.drawLine(a, b)
        p.setPen(QPen(QColor(BLUE), 1))
        p.setBrush(QColor(BLUE))
        for c, v, n in zip(sw.centre, sw.samples, sw.n_samples):
            if np.isfinite(v):
                rad = 2.5 if n < 5 else 4.0
                p.drawEllipse(QPointF(self.X(c), self.Y(v)), rad, rad)
        self.legend(p, [(AMBER, tr("estimate"), "line"), (theme.MUTED, tr("nearest neighbour"), "dash"),
                        (BLUE, tr("samples"), "dot")])


# --- the dialog ---------------------------------------------------------------------------------
class EstimationDialog(QDialog):
    def __init__(self, bm, main, attribute: str | None = None, parent=None, validate_only: bool = False):
        super().__init__(parent)
        self.bm = bm
        self.main = main
        self.samples = None
        self.pairs = None
        self._fields: dict = {}
        self._mask_cache = None              # (key, blocks inside the chosen solid)
        self.sample_spec: dict = {}
        self.written: str | None = None
        self.var_suggestion = None
        self.setWindowTitle(tr("Grade estimation - {name}", name=bm.info.get("name", "")))
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setWindowFlag(Qt.WindowMinMaxButtonsHint, True)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._samples_tab(), tr("1  Samples"))
        self.tabs.addTab(self._variogram_tab(), tr("2  Variogram"))
        self.tabs.addTab(self._estimate_tab(), tr("3  Estimate"))
        self.tabs.addTab(self._validate_tab(), tr("4  Validate"))
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        self.status = QLabel("")
        self.status.setStyleSheet(f"color: {theme.MUTED};")
        row.addWidget(self.status, 1)
        row.addWidget(close)
        v = QVBoxLayout(self)
        v.addWidget(self.tabs, 1)
        v.addLayout(row)
        self.resize(1000, 760)
        self._set_ready(False)
        if attribute and attribute in bm.attributes and not bm.is_categorical(attribute):
            self.target.setCurrentText(attribute)
            self.val_attr.setCurrentText(attribute)
        self._restore(attribute)
        if validate_only and self.samples is not None:
            self.tabs.setCurrentIndex(3)

    # --- tab 1: samples ------------------------------------------------------------------------
    def _scene_layers(self, cls_name):
        scene = getattr(self.main, "scene", None)
        return [lay for lay in (scene.layers if scene else []) if type(lay).__name__ == cls_name and lay.data is not None
                and not getattr(lay, "offline", None)]

    def _samples_tab(self) -> QWidget:
        self.source = QComboBox()
        for lay in self._scene_layers("DrillholeLayer"):
            self.source.addItem(icon("database-outline"), tr("Drillholes: {name}", name=lay.name), ("dh", lay.name))
        for lay in self._scene_layers("StringLayer"):
            self.source.addItem(icon("vector-polyline"), tr("Points: {name}", name=lay.name), ("str", lay.name))
        self.source.addItem(icon("folder-open-outline"), tr("Point string file (.str) ..."), ("file", ""))
        self.source.activated.connect(self._source_activated)
        self.source.currentIndexChanged.connect(self._source_changed)
        self.str_file: Path | None = None
        self.str_data = None
        self.table_box = QComboBox()
        self.table_box.currentIndexChanged.connect(self._fill_fields)
        self.field_box = QComboBox()
        self.composite = _spin(2.0, 0.0, 1000.0, 2, 0.5)
        self.composite.setToolTip(tr("Composite length down the hole in metres (0 = use the intervals as they are)"))
        self.min_cover = _ispin(50, 1, 100)
        self.min_cover.setSuffix(" %")
        self.dfield = QComboBox()
        self.neg_missing = QCheckBox(tr("Values below 0 are missing (e.g. -1 = not sampled)"))
        self.neg_missing.setChecked(True)
        self.top_cut = QLineEdit()
        self.top_cut.setPlaceholderText(tr("none"))
        sug_cut = QPushButton(tr("Suggest"))
        sug_cut.setToolTip(tr("Top cut at the 99th percentile when the highest values stand far above the rest"))
        sug_cut.clicked.connect(self._suggest_cut)
        cut_row = QHBoxLayout()
        cut_row.addWidget(self.top_cut, 1)
        cut_row.addWidget(sug_cut)
        self.domain = QComboBox()
        self.domain.addItem(tr("(all samples)"), None)
        for lay in solid_layers(getattr(self.main, "scene", None)):
            self.domain.addItem(lay.name, lay.name)
        load = QPushButton(icon("download-outline"), tr("Load samples"))
        load.clicked.connect(self.load_samples)
        self.stats = QLabel("")
        self.stats.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.stats.setWordWrap(True)
        form = QFormLayout()
        form.addRow(tr("Samples from"), self.source)
        self.lab_table = QLabel(tr("Table"))
        self.lab_field = QLabel(tr("Value field"))
        self.lab_comp = QLabel(tr("Composite length (m)"))
        self.lab_cover = QLabel(tr("Minimum composite cover"))
        self.lab_d = QLabel(tr("Value field"))
        form.addRow(self.lab_table, self.table_box)
        form.addRow(self.lab_field, self.field_box)
        form.addRow(self.lab_comp, self.composite)
        form.addRow(self.lab_cover, self.min_cover)
        form.addRow(self.lab_d, self.dfield)
        form.addRow("", self.neg_missing)
        form.addRow(tr("Top cut (cap)"), cut_row)
        form.addRow(tr("Domain: samples inside"), self.domain)
        w = QWidget()
        v = QVBoxLayout(w)
        v.addLayout(form)
        v.addWidget(load, 0, Qt.AlignLeft)
        v.addWidget(self.stats)
        v.addWidget(_muted(tr("Drillhole assays are composited to equal lengths down each hole (length-weighted "
                              "means), so long and short samples count fairly. Use the ore solid as the domain to "
                              "estimate the ore from ore samples only.")))
        v.addStretch(1)
        self._source_changed()
        return w

    def _source_activated(self, _i):
        kind, _name = self.source.currentData()
        if kind == "file":
            path, _ = QFileDialog.getOpenFileName(self, tr("Point string file"), str(self._folder()),
                                                  tr("Surpac strings (*.str)"))
            if path:
                self._set_str_file(Path(path))

    def _set_str_file(self, path: Path) -> bool:
        from ..io import read_str

        try:
            self.str_data = read_str(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, self.windowTitle(), str(e))
            return False
        self.str_file = path
        self.source.setItemText(self.source.currentIndex(), tr("Point string file: {name}", name=path.name))
        self._source_changed()
        return True

    def _folder(self) -> Path:
        ws = getattr(self.main, "workspace", None)
        return ws.folder if ws is not None else Path.cwd()

    def _layer(self, cls_name, name):
        return next((lay for lay in self._scene_layers(cls_name) if lay.name == name), None)

    def _source_changed(self, *_a):
        kind, name = self.source.currentData() or ("file", "")
        dh = kind == "dh"
        for w in (self.lab_table, self.table_box, self.lab_field, self.field_box, self.lab_comp, self.composite,
                  self.lab_cover, self.min_cover):
            w.setVisible(dh)
        for w in (self.lab_d, self.dfield):
            w.setVisible(not dh)
        if dh:
            lay = self._layer("DrillholeLayer", name)
            self._fields = bsamp.drillhole_fields(lay.data) if lay is not None else {}
            self.table_box.blockSignals(True)
            self.table_box.clear()
            for t in self._fields:
                self.table_box.addItem(t)
            if "assay" in self._fields:
                self.table_box.setCurrentText("assay")
            self.table_box.blockSignals(False)
            self._fill_fields()
        else:
            sf = self._string_file()
            n = bsamp.string_fields(sf) if sf is not None else 0
            self.dfield.clear()
            for i in range(1, max(n, 1) + 1):
                self.dfield.addItem(f"D{i}", i)

    def _fill_fields(self, *_a):
        self.field_box.clear()
        for f in self._fields.get(self.table_box.currentText(), []):
            self.field_box.addItem(f)

    def _string_file(self):
        kind, name = self.source.currentData() or ("file", "")
        if kind == "str":
            lay = self._layer("StringLayer", name)
            return lay.data if lay is not None else None
        return self.str_data

    def _spec(self) -> dict:
        kind, name = self.source.currentData() or ("file", "")
        cut = self.top_cut.text().strip()
        spec = {"source": kind, "layer": name, "negative_missing": self.neg_missing.isChecked(),
                "top_cut": float(cut) if cut else None, "domain": self.domain.currentData()}
        if kind == "dh":
            spec.update(table=self.table_box.currentText(), field=self.field_box.currentText(),
                        composite=self.composite.value(), min_cover=self.min_cover.value() / 100.0)
        else:
            spec.update(d_field=int(self.dfield.currentData() or 1),
                        file=str(self.str_file) if kind == "file" and self.str_file else None)
        return spec

    def _read_samples(self, spec: dict):
        null = self.bm.null_value
        if spec["source"] == "dh":
            lay = self._layer("DrillholeLayer", spec["layer"])
            if lay is None:
                raise ValueError(tr("The drillhole layer {name} is not open", name=spec["layer"]))
            if not spec.get("field"):
                raise ValueError(tr("Choose the value field"))
            s = bsamp.drillhole_samples(lay.data, spec["table"], spec["field"], spec["composite"] or None,
                                        spec.get("min_cover", 0.5), null, spec["negative_missing"])
        else:
            sf = self._string_file()
            if sf is None:
                raise ValueError(tr("Choose the point string file"))
            s = bsamp.string_samples(sf, spec["d_field"], null, spec["negative_missing"])
        if spec.get("domain"):
            lay = next((lay for lay in solid_layers(self.main.scene) if lay.name == spec["domain"]), None)
            if lay is None:
                raise ValueError(tr("The domain solid {name} is not open", name=spec["domain"]))
            s = s.subset(parallel.points_inside(s.xyz, *solid_mesh(lay)))
            if not s.n:
                raise ValueError(tr("No samples inside {name}", name=spec["domain"]))
        if spec.get("top_cut") is not None:
            s.value = np.minimum(s.value, spec["top_cut"])
        return s

    def _suggest_cut(self):
        try:
            s = self._read_samples({**self._spec(), "top_cut": None})
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, self.windowTitle(), str(e))
            return
        cut = bsamp.suggest_top_cut(s.value)
        if cut is None:
            self.top_cut.setText("")
            self.status.setText(tr("No top cut needed: the highest values are not far above the rest."))
        else:
            self.top_cut.setText(f"{cut:.4g}")
            self.status.setText(tr("Top cut {v:.4g} (99th percentile) suggested.", v=cut))

    def load_samples(self, quiet: bool = False) -> bool:
        spec = self._spec()

        def work(prog, _cancel):
            prog(None, tr("Reading the samples ..."))
            s = self._read_samples(spec)
            if s.n < 3:
                raise ValueError(tr("Too few samples ({n}) to estimate from.", n=s.n))
            prog(None, tr("Pairs for the variograms ..."))
            return s, Pairs(s.xyz, s.value), suggest_lag(s.xyz, s.hole, spec.get("composite"))

        res = run_with_progress(self, tr("Load samples"), work, cancellable=False)
        if isinstance(res, Exception):
            if not quiet:
                QMessageBox.warning(self, self.windowTitle(), str(res))
            return False
        s, self.pairs, (lag, nl) = res
        self.samples, self.sample_spec = s, spec
        st = bsamp.statistics(s.value, s.length)
        holes = len(set(s.hole[s.hole >= 0].tolist()))
        self.stats.setText(
            tr("{n} samples{holes}: mean {mean:.4g}, standard deviation {sd:.4g}, CV {cv:.2f}\n"
               "min {mn:.4g}, median {p50:.4g}, 90 % {p90:.4g}, 95 % {p95:.4g}, 99 % {p99:.4g}, max {mx:.4g}",
               n=fmt_num(st["n"]), holes=tr(" from {n} drillholes", n=holes) if holes else "", mean=st["mean"],
               sd=st["sd"], cv=st["cv"], mn=st["min"], p50=st["p50"], p90=st["p90"], p95=st["p95"], p99=st["p99"],
               mx=st["max"]))
        self.lag.setValue(lag)
        self.nlags.setValue(nl)
        if self.block_where.findData(spec.get("domain")) >= 0 and spec.get("domain"):
            self.block_where.setCurrentIndex(self.block_where.findData(spec.get("domain")))
        self._set_ready(True)
        self._update_variogram()
        self.status.setText(tr("{n} samples loaded.", n=fmt_num(s.n)))
        return True

    def _set_ready(self, ok: bool) -> None:
        for i in (1, 2, 3):
            self.tabs.setTabEnabled(i, ok)

    # --- tab 2: variogram -----------------------------------------------------------------------
    def _variogram_tab(self) -> QWidget:
        self.chart = VariogramChart()
        self.lag = _spin(10.0, 0.01, 10000, 2)
        self.nlags = _ispin(12, 3, 60)
        self.vdir = QComboBox()
        for key, text in (("omni", tr("Omnidirectional (ellipsoid distance)")), ("major", tr("Along the major axis")),
                          ("semi", tr("Along the semi-major axis")), ("minor", tr("Along the minor axis"))):
            self.vdir.addItem(text, key)
        for w in (self.lag, self.nlags):
            w.valueChanged.connect(self._update_variogram)
        self.vdir.currentIndexChanged.connect(self._update_variogram)
        self.nugget = _spin(0.0, 0.0, 1e9, 4)
        self.st = []
        grid = QGridLayout()
        grid.addWidget(QLabel(tr("Nugget")), 0, 0)
        grid.addWidget(self.nugget, 0, 1)
        for i in range(2):
            on = QCheckBox(tr("Structure {n}", n=i + 1))
            on.setChecked(i == 0)
            kind = QComboBox()
            for k, t in MODEL_TYPES.items():
                kind.addItem(tr(t), k)
            sill = _spin(1.0, 0.0, 1e9, 4)
            rng = _spin(100.0, 0.01, 1e6, 2)
            grid.addWidget(on, i + 1, 0)
            grid.addWidget(kind, i + 1, 1)
            grid.addWidget(QLabel(tr("Sill")), i + 1, 2)
            grid.addWidget(sill, i + 1, 3)
            grid.addWidget(QLabel(tr("Range (m)")), i + 1, 4)
            grid.addWidget(rng, i + 1, 5)
            self.st.append((on, kind, sill, rng))
            for w in (sill, rng):
                w.valueChanged.connect(self._update_variogram)
            kind.currentIndexChanged.connect(self._update_variogram)
            on.toggled.connect(self._update_variogram)
        self.nugget.valueChanged.connect(self._update_variogram)
        self.va = self._aniso_fields(self._update_variogram)
        arow = QHBoxLayout()
        for lab, w in zip((tr("Bearing"), tr("Plunge"), tr("Dip"), tr("Semi / major"), tr("Minor / major")), self.va):
            arow.addWidget(QLabel(lab))
            arow.addWidget(w)
        sug = QPushButton(icon("auto-fix"), tr("Suggest model (auto fit)"))
        sug.setToolTip(tr("Fit the model to the experimental variograms: nugget, structures, ranges and the "
                          "direction of longest continuity"))
        sug.clicked.connect(self.suggest_variogram)
        self.vnotes = FindingList()
        top = QHBoxLayout()
        for lab, w in ((tr("Lag (m)"), self.lag), (tr("Lags"), self.nlags), (tr("Direction"), self.vdir)):
            top.addWidget(QLabel(lab))
            top.addWidget(w)
        top.addStretch(1)
        top.addWidget(sug)
        box = QGroupBox(tr("Variogram model"))
        bl = QVBoxLayout(box)
        bl.addLayout(grid)
        bl.addLayout(arow)
        w = QWidget()
        v = QVBoxLayout(w)
        v.addLayout(top)
        v.addWidget(self.chart, 1)
        v.addWidget(box)
        v.addWidget(self.vnotes)
        return w

    def _aniso_fields(self, slot):
        fields = (_spin(0.0, -360, 360, 1, 5), _spin(0.0, -90, 90, 1, 5), _spin(0.0, -90, 90, 1, 5),
                  _spin(1.0, 0.01, 1.0, 2, 0.05), _spin(1.0, 0.01, 1.0, 2, 0.05))
        for f in fields:
            f.valueChanged.connect(slot)
        return fields

    @staticmethod
    def _aniso_of(fields) -> Anisotropy:
        b, p, d, r1, r2 = (f.value() for f in fields)
        return Anisotropy(b, p, d, r1, min(r2, r1) if r2 > r1 else r2)

    @staticmethod
    def _set_aniso(fields, a: Anisotropy) -> None:
        for f, v in zip(fields, (a.bearing, a.plunge, a.dip, a.ratio1, a.ratio2)):
            f.blockSignals(True)
            f.setValue(v)
            f.blockSignals(False)

    def model(self) -> VariogramModel:
        sts = [Structure(k.currentData(), s.value(), r.value()) for on, k, s, r in self.st if on.isChecked()]
        return VariogramModel(self.nugget.value(), sts or [Structure()], self._aniso_of(self.va))

    def set_model(self, m: VariogramModel) -> None:
        widgets = [self.nugget, *[w for row in self.st for w in row]]
        for w in widgets:
            w.blockSignals(True)
        self.nugget.setValue(m.nugget)
        for i, (on, k, s, r) in enumerate(self.st):
            on.setChecked(i < len(m.structures))
            if i < len(m.structures):
                st = m.structures[i]
                k.setCurrentIndex(max(0, k.findData(st.kind)))
                s.setValue(st.sill)
                r.setValue(st.range)
        for w in widgets:
            w.blockSignals(False)
        self._set_aniso(self.va, m.anisotropy)
        if hasattr(self, "same_ellipsoid"):           # the search ellipsoid follows (Estimate tab)
            self._sync_search()
        self._update_variogram()

    def _update_variogram(self, *_a) -> None:
        if self.pairs is None:
            return
        m = self.model()
        key = self.vdir.currentData()
        edges = lag_edges(self.lag.value(), self.nlags.value(), self.sample_spec.get("composite") or None)
        axes = m.anisotropy.axes()
        if key == "omni":
            e = self.pairs.bin(edges, aniso=m.anisotropy)
            scale = 1.0
        else:
            i = {"major": 0, "semi": 1, "minor": 2}[key]
            e = self.pairs.bin(edges, direction=axes[i], tol_deg=22.5)
            scale = [1.0, 1.0 / max(m.anisotropy.ratio1, 1e-6), 1.0 / max(m.anisotropy.ratio2, 1e-6)][i]
        hmax = float(edges[-1])
        h = np.linspace(0, hmax, 200)
        self.chart.set_data(e, h, m.gamma(h * scale), m.sill, float(np.var(self.samples.value)))

    def suggest_variogram(self) -> None:
        s = self.samples
        if s is None:
            return
        lag, nl = self.lag.value(), self.nlags.value()
        comp = self.sample_spec.get("composite") or None
        res = run_with_progress(self, tr("Fitting the variogram ..."),
                                lambda prog, cancel: parallel.run(suggest_model, s.xyz, s.value, s.hole, comp, lag, nl,
                                                                  progress=prog, cancel=cancel,
                                                                  text=tr("Fitting the variogram ...")))
        if isinstance(res, Cancelled):
            return
        if isinstance(res, Exception):
            QMessageBox.warning(self, self.windowTitle(), str(res))
            return
        self.var_suggestion = res
        self.set_model(res.model)
        self.vnotes.set_findings([("info", n) for n in res.notes])
        self._sync_search()

    # --- tab 3: estimate -------------------------------------------------------------------------
    def _estimate_tab(self) -> QWidget:
        self.target = QComboBox()
        self.target.setEditable(True)
        for n in self.bm.attributes:
            if not self.bm.is_categorical(n) and n not in DEPLETION_ATTRS:
                self.target.addItem(n)
        self.target.setToolTip(tr("The attribute to estimate; type a new name to add an attribute"))
        self.method = QComboBox()
        for k, t in METHODS.items():
            self.method.addItem(tr(t), k)
        self.method.currentIndexChanged.connect(self._method_changed)
        self.power = _spin(2.0, 0.5, 6.0, 1, 0.5)
        self.sk_mean = QLineEdit()
        self.sk_mean.setPlaceholderText(tr("sample mean"))
        self.same_ellipsoid = QCheckBox(tr("Same as the variogram anisotropy"))
        self.same_ellipsoid.setChecked(True)
        self.same_ellipsoid.toggled.connect(self._sync_search)
        self.sa = self._aniso_fields(lambda *_a: None)
        for f in self.va:                   # an edit on the Variogram tab shows here at once
            f.valueChanged.connect(self._sync_search)
        srow = QGridLayout()
        for i, (lab, w) in enumerate(zip((tr("Bearing"), tr("Plunge"), tr("Dip"), tr("Semi / major"),
                                          tr("Minor / major")), self.sa)):
            srow.addWidget(QLabel(lab), i // 3, 2 * (i % 3))
            srow.addWidget(w, i // 3, 2 * (i % 3) + 1)
        self.radius = _spin(100.0, 0.1, 1e6, 1)
        self.kmin = _ispin(4, 1, 64)
        self.kmax = _ispin(16, 1, 128)
        self.per_hole = _ispin(0, 0, 64)
        self.per_hole.setSpecialValueText(tr("no limit"))
        self.passes = _ispin(2, 1, 3)
        self.pass_factor = _spin(2.0, 1.1, 5.0, 1, 0.5)
        self.disc = (_ispin(3, 1, 10), _ispin(3, 1, 10), _ispin(2, 1, 10))
        drow = QHBoxLayout()
        for lab, w in zip("XYZ", self.disc):
            drow.addWidget(QLabel(lab))
            drow.addWidget(w)
        drow.addStretch(1)
        self.block_where = QComboBox()
        self.block_where.addItem(tr("All blocks"), None)
        for lay in solid_layers(getattr(self.main, "scene", None)):
            self.block_where.addItem(tr("Inside {name}", name=lay.name), lay.name)
        self.unestimated = QComboBox()
        self.unestimated.addItem(tr("Set to null"), "null")
        self.unestimated.addItem(tr("Keep their current value"), "keep")
        self.out_checks = {}
        orow = QGridLayout()
        for i, key in enumerate(("ns", "dist", "pass", "var", "ke", "slope")):
            cb = QCheckBox(tr(OUTPUTS[key]))
            cb.setChecked(key in ("ns", "var", "slope"))
            self.out_checks[key] = cb
            orow.addWidget(cb, i // 3, i % 3)
        self.out_hint = _muted("")
        self.target.currentTextChanged.connect(self._outputs_hint)
        form = QFormLayout()
        form.addRow(tr("Attribute to estimate"), self.target)
        form.addRow(tr("Method"), self.method)
        self.lab_power = QLabel(tr("Power"))
        form.addRow(self.lab_power, self.power)
        self.lab_mean = QLabel(tr("Simple kriging mean"))
        form.addRow(self.lab_mean, self.sk_mean)
        sbox = QGroupBox(tr("Search"))
        sf = QFormLayout(sbox)
        sf.addRow(tr("Ellipsoid"), self.same_ellipsoid)
        sf.addRow("", srow)
        sf.addRow(tr("Radius along the major axis (m)"), self.radius)
        sf.addRow(tr("Minimum samples"), self.kmin)
        sf.addRow(tr("Maximum samples"), self.kmax)
        sf.addRow(tr("Maximum samples per drillhole"), self.per_hole)
        prow = QHBoxLayout()
        prow.addWidget(self.passes)
        prow.addWidget(QLabel(tr("each pass enlarges the ellipsoid x")))
        prow.addWidget(self.pass_factor)
        prow.addStretch(1)
        sf.addRow(tr("Search passes"), prow)
        sug = QPushButton(icon("auto-fix"), tr("Suggest search (cross-validation)"))
        sug.setToolTip(tr("Try many combinations of radius, minimum / maximum samples and samples per hole: each "
                          "sample is estimated without its own drillhole and the most accurate choice is kept"))
        sug.clicked.connect(self.suggest_search)
        sf.addRow("", sug)
        bbox = QGroupBox(tr("Blocks"))
        bf = QFormLayout(bbox)
        bf.addRow(tr("Estimate"), self.block_where)
        bf.addRow(tr("Blocks not estimated"), self.unestimated)
        self.lab_disc = QLabel(tr("Discretisation (points per block)"))
        bf.addRow(self.lab_disc, drow)
        bf.addRow(tr("Also store"), orow)
        bf.addRow("", self.out_hint)
        self.snotes = FindingList()
        run = QPushButton(icon("play"), tr("Run estimation"))
        run.setStyleSheet(f"QPushButton {{ background: {theme.ACCENT_DIM}; font-weight: 600; padding: 6px 16px; }}")
        run.clicked.connect(self.run_estimation)
        inner = QWidget()
        v = QVBoxLayout(inner)
        v.addLayout(form)
        v.addWidget(sbox)
        v.addWidget(bbox)
        v.addWidget(self.snotes)
        v.addWidget(run, 0, Qt.AlignRight)
        v.addStretch(1)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(inner)
        self._method_changed()
        self._sync_search()
        self._outputs_hint()
        return area

    def _outputs_hint(self, *_a) -> None:
        t = self.target.currentText().strip() or "?"
        self.out_hint.setText(tr("Stored as {names}", names=", ".join(f"{t}{OUTPUT_SUFFIX[k]}" for k in OUTPUT_SUFFIX)))

    def _method_changed(self, *_a) -> None:
        m = self.method.currentData()
        krig = m in ("ok", "sk")
        for w in (self.lab_power, self.power):
            w.setVisible(m == "idw")
        for w in (self.lab_mean, self.sk_mean):
            w.setVisible(m == "sk")
        for w in (self.lab_disc, *self.disc):
            w.setEnabled(krig)
        for k in KRIGING_ONLY:
            self.out_checks[k].setEnabled(krig)

    def _sync_search(self, *_a) -> None:
        same = self.same_ellipsoid.isChecked()
        for f in self.sa:
            f.setEnabled(not same)
        if same:
            self._set_aniso(self.sa, self._aniso_of(self.va))

    def params(self) -> EstimationParams:
        self._sync_search()
        mean = self.sk_mean.text().strip()
        return EstimationParams(
            method=self.method.currentData(), power=self.power.value(), model=self.model(),
            search=self._aniso_of(self.sa), radius=self.radius.value(), min_samples=self.kmin.value(),
            max_samples=max(self.kmax.value(), self.kmin.value()), max_per_hole=self.per_hole.value(),
            discretisation=tuple(w.value() for w in self.disc), passes=self.passes.value(),
            pass_factor=self.pass_factor.value(), sk_mean=float(mean) if mean else None)

    def set_params(self, p: EstimationParams) -> None:
        self.method.setCurrentIndex(max(0, self.method.findData(p.method)))
        self.power.setValue(p.power)
        self.sk_mean.setText("" if p.sk_mean is None else f"{p.sk_mean:g}")
        self.set_model(p.model)
        same = p.search.to_dict() == p.model.anisotropy.to_dict()
        self.same_ellipsoid.setChecked(same)
        self._set_aniso(self.sa, p.search)
        self.radius.setValue(p.radius)
        self.kmin.setValue(p.min_samples)
        self.kmax.setValue(p.max_samples)
        self.per_hole.setValue(p.max_per_hole)
        self.passes.setValue(p.passes)
        self.pass_factor.setValue(p.pass_factor)
        for w, v in zip(self.disc, p.discretisation):
            w.setValue(int(v))

    def suggest_search(self) -> None:
        if self.samples is None:
            return
        p = self.params()
        s = self.samples
        res = run_with_progress(self, tr("Cross-validating search parameters ..."),
                                lambda prog, cancel: parallel.suggest_search(s, p, progress=prog, cancel=cancel))
        if isinstance(res, Cancelled):
            return
        if isinstance(res, Exception):
            QMessageBox.warning(self, self.windowTitle(), str(res))
            return
        self.radius.setValue(res.radius)
        self.kmin.setValue(res.min_samples)
        self.kmax.setValue(res.max_samples)
        self.per_hole.setValue(res.max_per_hole)
        if p.method == "idw":
            self.power.setValue(res.power)
        notes = [("info", n) for n in res.notes]
        notes.insert(0, ("ok", tr("Suggested: radius {r:.1f} m, {kmin} to {kmax} samples{hole}{pw}.",
                                  r=res.radius, kmin=res.min_samples, kmax=res.max_samples,
                                  hole=tr(", at most {n} per drillhole", n=res.max_per_hole) if res.max_per_hole else "",
                                  pw=tr(", power {p:g}", p=res.power) if p.method == "idw" else "")))
        self.snotes.set_findings(notes)

    def _block_mask_job(self):
        """A function (progress, cancel) -> the blocks to estimate (all, or inside the chosen
        solid), for a background job. Raises ValueError for a solid that is not open."""
        name = self.block_where.currentData()
        n = self.bm.n_blocks
        if not name:
            return lambda _p, _c: np.ones(n, dtype=bool)
        lay = next((lay for lay in solid_layers(self.main.scene) if lay.name == name), None)
        if lay is None:
            raise ValueError(tr("The solid {name} is not open", name=name))
        key = (name, id(lay.data), id(self.bm.centroids), n)
        if self._mask_cache is not None and self._mask_cache[0] == key:
            mask = self._mask_cache[1]
            return lambda _p, _c: mask
        mesh = solid_mesh(lay)
        cents = self.bm.centroids

        def job(prog, cancel):
            mask = parallel.points_inside(cents, *mesh, progress=prog, cancel=cancel)
            self._mask_cache = (key, mask)
            return mask

        return job

    def run_estimation(self) -> None:
        from ..blockmodel.build import attribute_name_ok

        bm = self.bm
        target = self.target.currentText().strip()
        if target not in bm.attributes:
            why = attribute_name_ok(bm, target)
            if why:
                QMessageBox.warning(self, self.windowTitle(), why)
                return
        elif bm.is_categorical(target):
            QMessageBox.warning(self, self.windowTitle(), tr("{name} is a text attribute", name=target))
            return
        p = self.params()
        outs = ["value"] + [k for k, cb in self.out_checks.items() if cb.isChecked() and cb.isEnabled()]
        names = {k: target + OUTPUT_SUFFIX[k] for k in outs if k != "value"}
        for k, n in names.items():
            why = attribute_name_ok(bm, n) if n not in bm.attributes else ""
            if why:
                QMessageBox.warning(self, self.windowTitle(), f"{n}: {why}")
                return
        try:
            mask_job = self._block_mask_job()
        except ValueError as e:
            QMessageBox.warning(self, self.windowTitle(), str(e))
            return
        s = self.samples

        def work(prog, cancel):
            ids = np.flatnonzero(mask_job(prog, cancel))
            prog(0.0, tr("Estimating {n} blocks ...", n=fmt_num(len(ids))))
            return ids, parallel.estimate(bm.centroids[ids], bm.sizes[ids], s, p, outs, progress=prog, cancel=cancel)

        res = run_with_progress(self, tr("Estimating {name} ...", name=target), work)
        if isinstance(res, Cancelled) or res is None:
            self.status.setText(tr("Estimation cancelled: the block model was not changed."))
            return
        if isinstance(res, Exception):
            QMessageBox.warning(self, self.windowTitle(), str(res))
            return
        ids, res = res
        keep = self.unestimated.currentData() == "keep"
        null = np.float32(bm.null_value)
        for k in outs:
            name = target if k == "value" else names[k]
            if name not in bm.attributes:
                bm.attributes[name] = np.full(bm.n_blocks, null, dtype=np.float32)
            a = bm.attributes[name]
            v = res[k]
            got = np.isfinite(v)
            if k == "value" and keep:
                a[ids[got]] = v[got].astype(np.float32)
            else:
                a[ids] = np.where(got, v, null).astype(np.float32)
        n_est = int(np.isfinite(res["value"]).sum())
        desc = bm.info.setdefault("descriptions", {})
        for k, n in names.items():
            desc.setdefault(n, tr("{what} of {name}", what=tr(OUTPUTS[k]), name=target))
        bm.info.setdefault("estimations", {})[target] = {
            "when": datetime.now().isoformat(timespec="seconds"), "samples": self.sample_spec,
            "params": p.to_dict(), "blocks": self.block_where.currentData(), "outputs": outs,
            "n_estimated": n_est, "n_blocks": int(len(ids))}
        add_history(bm, f"Estimated {target} by {p.method.upper()} from {s.source} ({s.n:,} samples): "
                        f"{n_est:,} of {len(ids):,} blocks, radius {p.radius:g} m, {p.min_samples}-{p.max_samples} "
                        f"samples")
        self.written = target
        if self.target.findText(target) < 0:
            self.target.addItem(target)
        if self.val_attr.findText(target) < 0:
            self.val_attr.addItem(target)
        self.val_attr.setCurrentText(target)
        self.status.setText(tr("{name}: {n} of {total} blocks estimated. Check the result on the Validate tab, "
                               "then Save in the manager.", name=target, n=fmt_num(n_est), total=fmt_num(len(ids))))
        self.tabs.setCurrentIndex(3)
        self.run_validation()

    # --- tab 4: validate --------------------------------------------------------------------------
    def _validate_tab(self) -> QWidget:
        self.val_attr = QComboBox()
        for n in self.bm.attributes:
            if not self.bm.is_categorical(n) and n not in DEPLETION_ATTRS:
                self.val_attr.addItem(n)
        run = QPushButton(icon("check-decagram-outline"), tr("Validate"))
        run.setToolTip(tr("Cross-validation, global bias, smoothing and swath plots of the estimated attribute, "
                          "with the samples and search of the tabs before"))
        run.clicked.connect(self.run_validation)
        self.swath_axis = QComboBox()
        for i, a in enumerate("XYZ"):
            self.swath_axis.addItem(tr("Swath along {axis}", axis=a), i)
        self.swath_axis.currentIndexChanged.connect(self._show_swath)
        self.scatter = ScatterChart()
        self.swath_chart = SwathChart()
        self.vfindings = FindingList()
        self.vtable = QLabel("")
        self.vtable.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.vtable.setWordWrap(True)
        top = QHBoxLayout()
        top.addWidget(QLabel(tr("Attribute")))
        top.addWidget(self.val_attr, 1)
        top.addWidget(run)
        charts = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(QLabel(tr("Cross-validation: true value against estimate")))
        left.addWidget(self.scatter, 1)
        right = QVBoxLayout()
        right.addWidget(self.swath_axis)
        right.addWidget(self.swath_chart, 1)
        charts.addLayout(left, 1)
        charts.addLayout(right, 1)
        inner = QWidget()
        v = QVBoxLayout(inner)
        v.addLayout(top)
        v.addLayout(charts, 1)
        v.addWidget(self.vtable)
        v.addWidget(self.vfindings)
        v.addWidget(_muted(tr("Cross-validation estimates every sample from the others, leaving out its own "
                              "drillhole. The nearest neighbour model is a declustered reference: the estimate's "
                              "mean should be within a few % of it, and its slices should follow the samples.")))
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(inner)
        self._swaths = []
        return area

    def run_validation(self) -> None:
        if self.samples is None:
            QMessageBox.information(self, self.windowTitle(), tr("Load the samples first (tab 1)."))
            return
        name = self.val_attr.currentText()
        if name not in self.bm.attributes:
            return
        bm = self.bm
        p = self.params()
        s = self.samples
        vals = bm.attributes[name].astype(np.float64)
        vals = np.where(bm.valid_mask(name), vals, np.nan)
        try:
            mask_job = self._block_mask_job()
        except ValueError:
            mask_job = None
        if not np.isfinite(vals).any():
            QMessageBox.information(self, self.windowTitle(), tr("{name} has no values to validate.", name=name))
            return

        def work(prog, cancel):
            if mask_job is not None:
                v = np.where(mask_job(prog, cancel), vals, np.nan)
            else:
                v = vals
            ids = np.flatnonzero(np.isfinite(v))
            if not len(ids):
                raise ValueError(tr("{name} has no values to validate.", name=name))
            cv = parallel.run(bval.cross_validation, s, p, progress=prog, cancel=cancel,
                              text=tr("Cross-validation ..."))
            prog(0.0, tr("Nearest neighbour reference ..."))
            nn = parallel.estimate(bm.centroids[ids], bm.sizes[ids], s, bval.nn_params(p), ("value",),
                                   progress=prog, cancel=cancel)["value"]
            vol = np.prod(bm.sizes[ids].astype(np.float64), axis=1)
            g = bval.global_check(v[ids], nn, vol, s)
            prog(None, tr("Swath plots ..."))
            sw = [bval.swath(a, bm.centroids[ids], v[ids], nn, vol, s) for a in range(3)]
            return cv, g, sw

        res = run_with_progress(self, tr("Validating {name} ...", name=name), work)
        if isinstance(res, Cancelled):
            return
        if isinstance(res, Exception):
            QMessageBox.warning(self, self.windowTitle(), str(res))
            return
        cv, g, sw = res
        self._swaths = sw
        self.scatter.set_data(cv.est, cv.true, cv.stats.get("slope"))
        self._show_swath()
        self.vfindings.set_findings(bval.findings(cv, g, sw))
        st = cv.stats
        rows = [tr("Blocks: {n}   Estimate mean {e:.4g}   Nearest neighbour mean {nn:.4g}   Sample mean {s:.4g}",
                   n=fmt_num(g.n_blocks), e=g.mean_est, nn=g.mean_nn, s=g.mean_samples),
                tr("CV of blocks {a:.2f}, of nearest neighbour {b:.2f}, of samples {c:.2f}",
                   a=g.cv_est, b=g.cv_nn, c=g.cv_samples)]
        if st.get("n", 0) >= 3:
            rows.append(tr("Cross-validation: {n} of {t} samples estimated   mean error {me:+.4g}   MAE {mae:.4g}   "
                           "RMSE {rmse:.4g}   correlation {c:.2f}   slope {sl:.2f}", n=st["n"], t=st["tested"],
                           me=st["me"], mae=st["mae"], rmse=st["rmse"], c=st["corr"], sl=st["slope"]))
        self.vtable.setText("\n".join(rows))
        rec = bm.info.setdefault("validations", {})
        rec[name] = {"when": datetime.now().isoformat(timespec="seconds"),
                     "cv": {k: (float(v) if isinstance(v, (int, float, np.floating)) else v) for k, v in st.items()},
                     "global": {k: float(v) for k, v in vars(g).items()}}
        self.status.setText(tr("Validation of {name} done.", name=name))

    def _show_swath(self, *_a) -> None:
        i = self.swath_axis.currentData() or 0
        self.swath_chart.set_swath(self._swaths[i] if self._swaths else None)

    # --- the settings of the last estimation ---------------------------------------------------
    def _restore(self, attribute: str | None) -> None:
        rec = (self.bm.info.get("estimations") or {}).get(attribute) if attribute else None
        if not rec:
            return
        spec = rec.get("samples") or {}
        kind = spec.get("source")
        for i in range(self.source.count()):
            data = self.source.itemData(i)
            if data and data[0] == kind and (kind == "file" or data[1] == spec.get("layer")):
                self.source.setCurrentIndex(i)
                break
        if kind == "file" and spec.get("file") and Path(spec["file"]).exists():
            self._set_str_file(Path(spec["file"]))
        if kind == "dh":
            self.table_box.setCurrentText(spec.get("table", ""))
            self.field_box.setCurrentText(spec.get("field", ""))
            self.composite.setValue(spec.get("composite") or 0.0)
            self.min_cover.setValue(int(round(100 * spec.get("min_cover", 0.5))))
        elif spec.get("d_field"):
            self.dfield.setCurrentIndex(max(0, self.dfield.findData(spec["d_field"])))
        self.neg_missing.setChecked(bool(spec.get("negative_missing", True)))
        self.top_cut.setText("" if spec.get("top_cut") is None else f"{spec['top_cut']:g}")
        if spec.get("domain") and self.domain.findData(spec["domain"]) >= 0:
            self.domain.setCurrentIndex(self.domain.findData(spec["domain"]))
        loaded = self.load_samples(quiet=True)
        try:
            self.set_params(EstimationParams.from_dict(rec.get("params") or {}))
        except Exception:  # noqa: BLE001 - an older record
            pass
        if rec.get("blocks") and self.block_where.findData(rec["blocks"]) >= 0:
            self.block_where.setCurrentIndex(self.block_where.findData(rec["blocks"]))
        for k, cb in self.out_checks.items():
            cb.setChecked(k in (rec.get("outputs") or []))
        if loaded:
            self.status.setText(tr("Settings of the last estimation of {name} ({when}).", name=attribute,
                                   when=str(rec.get("when", "")).replace("T", " ")))
