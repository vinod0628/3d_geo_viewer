import os

import numpy as np
import pandas as pd
import pytest

from geoviewer.io import read_drillholes_csv, read_drillholes_sqlite
from geoviewer.io.drillholes import DrillholeDB, _direction


@pytest.fixture(scope="module")
def surpac_db(surpac_sqlite):
    """The demo drillholes, read from Surpac-style tables (collar, survey, geology, sample, weathering)."""
    return read_drillholes_sqlite(surpac_sqlite)


def test_surpac_tables(surpac_db):
    db = surpac_db
    assert len(db.collars) == 94
    assert len(db.surveys) == 163          # demo data: 14 holes made inclined / curved (2026-11-21)
    assert {"geology", "weathering", "sample"} <= set(db.intervals)
    assert not db.warnings
    assert db.dip_positive_down is False
    assert db.code_descriptions("geology", "rock")["B"] == "Basalt"


def test_weathering_from_depths_are_filled(surpac_db):
    w = surpac_db.intervals["weathering"]
    h = w[w.hole_id == "WD004"]
    assert h.depth_from.tolist()[:2] == [0.0, 6.0]
    assert h.depth_to.tolist()[:2] == [6.0, 74.0]


def test_trace_starts_at_collar_and_preserves_length(surpac_db):
    db = surpac_db
    for hid in db.hole_ids[:20]:
        tr = db.trace(hid)
        np.testing.assert_allclose(tr.positions_at([0.0])[0], tr.collar)
        # Along-hole distance is never shorter than the straight line to the end.
        end = tr.positions_at([tr.md[-1]])[0]
        assert np.linalg.norm(end - tr.collar) <= tr.md[-1] + 1e-6
        # Station positions agree with interpolated positions at station depths.
        np.testing.assert_allclose(tr.positions_at(tr.md), tr.xyz, atol=1e-6)


def test_nearly_vertical_hole(surpac_db):
    tr = surpac_db.trace("WD004")  # dips -90 .. -87
    p = tr.positions_at([100.0])[0]
    assert -100.0 <= p[2] - tr.collar[2] < -99.0
    assert np.linalg.norm(p[:2] - tr.collar[:2]) < 6.0


def test_min_curvature_on_circular_arc():
    """A hole turning 90 degrees over a quarter circle ends where the arc ends."""
    r = 100.0
    md = np.linspace(0, np.pi / 2 * r, 10)
    dip = -90.0 + np.degrees(md / r)  # from vertical down to horizontal (east)
    collar = pd.DataFrame({"hole_id": ["A"], "x": [0.0], "y": [0.0], "z": [0.0], "max_depth": [md[-1]]})
    survey = pd.DataFrame({"hole_id": "A", "depth": md, "azimuth": 90.0, "dip": dip})
    db = DrillholeDB(collar, survey, {})
    tr = db.trace("A")
    np.testing.assert_allclose(tr.positions_at([md[-1]])[0], [r, 0, -r], atol=1e-6)
    # Mid-segment point lies on the circle too.
    q = (md[3] + md[4]) / 2
    p = tr.positions_at([q])[0]
    np.testing.assert_allclose(np.hypot(p[0] - r, p[2]), r, atol=1e-6)


def test_direction_convention():
    np.testing.assert_allclose(_direction(np.array([0.0]), np.array([0.0]))[0], [0, 1, 0], atol=1e-12)
    np.testing.assert_allclose(_direction(np.array([90.0]), np.array([0.0]))[0], [1, 0, 0], atol=1e-12)
    np.testing.assert_allclose(_direction(np.array([0.0]), np.array([-90.0]))[0], [0, 0, -1], atol=1e-12)


def test_csv_matches_the_database(surpac_db, tmp_path):
    """Exporting the drillhole tables to CSV and reading them back gives the same traces."""
    db = surpac_db
    db.collars.to_csv(tmp_path / "collar.csv", index=False)
    db.surveys.to_csv(tmp_path / "survey.csv", index=False)
    db.intervals["geology"].to_csv(tmp_path / "lithology.csv", index=False)
    csv = read_drillholes_csv(
        tmp_path / "collar.csv", tmp_path / "survey.csv", {"lithology": tmp_path / "lithology.csv"}
    )
    _, f1, t1 = db.interval_positions("geology")
    _, f2, t2 = csv.interval_positions("lithology")
    np.testing.assert_allclose(f1, f2)
    np.testing.assert_allclose(t1, t2)
    assert not csv.warnings


def test_intervals_follow_a_curved_hole(surpac_db):
    """Regression: intervals were straight chords from 'from' to 'to', cutting across the bend
    of a curved hole (the grey trace showed beside them). They follow the path now."""
    db = surpac_db
    rows, pts, deps, counts = db.interval_paths("geology")
    df = db.intervals["geology"]
    tr = db.trace("WD033")
    sv = db.surveys[db.surveys.hole_id == "WD033"]
    assert len(sv) > 2 and sv.azimuth.nunique() > 1      # the demo hole is curved
    starts = np.r_[0, np.cumsum(counts)[:-1]]
    checked = 0
    for r, s, k in zip(rows, starts, counts):
        if df.hole_id.iloc[r] != "WD033":
            continue
        d = deps[s:s + k]
        assert d[0] == df.depth_from.iloc[r] and d[-1] == df.depth_to.iloc[r]
        np.testing.assert_allclose(pts[s:s + k], tr.positions_at(d), atol=1e-9)   # on the path
        if d[-1] - d[0] > 4.0:
            assert k > 2                                    # long intervals are subdivided
            mid = (pts[s:s + k - 1] + pts[s + 1:s + k]) / 2
            on_path = tr.positions_at((d[:-1] + d[1:]) / 2)
            assert np.abs(mid - on_path).max() < 0.05       # no chord across the bend
        checked += 1
    assert checked > 0
    # straight (vertical) holes keep two points per interval
    r_v = [i for i, r in enumerate(rows) if df.hole_id.iloc[r] == "WRC094"]
    assert r_v and all(counts[i] == 2 for i in r_v)


def test_hole_log_for_the_identify_window(surpac_db):
    from geoviewer.scene.layers import DrillholeLayer

    lay = DrillholeLayer(name="surpac", path=None, data=surpac_db)
    log = lay.hole_log("WD033")
    tr = surpac_db.trace("WD033")
    np.testing.assert_allclose(log.collar, tr.collar)
    assert log.length == pytest.approx(120.0)
    assert log.depths[0] == 0.0 and log.depths[-1] == pytest.approx(120.0)
    np.testing.assert_allclose(log.path[0], tr.collar)                 # path starts at the collar
    assert [iv[2] for iv in log.intervals] == ["SH", "ST", "QV1", "ST"]
    assert log.intervals[0][3] == "Shale"                               # code description
    assert log.intervals[0][4] == tuple(lay.code_color("SH"))           # colour as in 3D
    assert (log.collar_azimuth, log.collar_dip) == (pytest.approx(90.0), pytest.approx(-60.0))


def test_drillhole_log_page_and_print(surpac_db, tmp_path):
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QApplication

    from geoviewer.scene.layers import DrillholeLayer
    from geoviewer.ui.drillhole_log import DrillholeLogDialog

    app = QApplication.instance() or QApplication([])
    lay = DrillholeLayer(name="surpac", path=None, data=surpac_db)
    dlg = DrillholeLogDialog()
    for hid in ("WD033", "WRC094"):                   # curved and vertical
        dlg.show_hole(lay.hole_log(hid), 40.0)
        app.processEvents()
        assert dlg.windowTitle() == f"Drillhole {hid}"
        img = dlg.page_image(150)
        assert (img.width(), img.height()) == (1754, 1240)              # A4 landscape at 150 dpi
        assert img.pixelColor(3, 3) == QColor("white")
        dark = sum(img.pixelColor(x, y).lightness() < 128 for x in range(0, img.width(), 25)
                   for y in range(0, img.height(), 25))
        assert dark > 10                                                  # something was drawn
    from PySide6.QtPrintSupport import QPrinter

    pr = QPrinter(QPrinter.HighResolution)
    pr.setOutputFormat(QPrinter.PdfFormat)
    pdf = tmp_path / "log.pdf"
    pr.setOutputFileName(str(pdf))
    dlg.print_to(pr)
    assert pdf.exists() and pdf.stat().st_size > 2000
    dlg.close()


def test_drillhole_log_click_moves_the_depth_marker(surpac_db):
    """Clicking the strip log or the path in the XZ / XY pane moves the marker in all views."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from geoviewer.scene.layers import DrillholeLayer
    from geoviewer.ui.drillhole_log import DrillholeLogDialog

    app = QApplication.instance() or QApplication([])
    lay = DrillholeLayer(name="surpac", path=None, data=surpac_db)
    dlg = DrillholeLogDialog()
    dlg.resize(1100, 760)
    dlg.show_hole(lay.hole_log("WD033"), 59.6)
    dlg.view.grab()                                   # paints: the layout is known now
    app.processEvents()
    v, lay_info = dlg.view, dlg.view.layout_info
    st = lay_info["strip"]
    seen = []
    v.depth_picked.connect(seen.append)
    # strip log at 30 m
    y30 = st["top"] + 30.0 / st["length"] * st["h"]
    QTest.mouseClick(v, Qt.LeftButton, Qt.NoModifier, QPoint(int(st["column"].center().x()), int(round(y30))))
    assert v.pick_depth == pytest.approx(30.0, abs=0.5) and seen
    from geoviewer.ui.drillhole_log import header_lines
    head = header_lines(v.log, v.pick_depth)
    assert head[1].startswith("Depth 30") and "Schist" in head[1]
    # XZ pane: the path point at ~100 m
    xz, xy = lay_info["panes"]
    k = int(np.searchsorted(v.log.depths, 100.0))
    px, py = xz["pts"][k]
    QTest.mouseClick(v, Qt.LeftButton, Qt.NoModifier, QPoint(int(round(px)), int(round(py))))
    assert v.pick_depth == pytest.approx(v.log.depths[k], abs=1.5)
    # XY pane, a few pixels beside the path at ~15 m
    k = int(np.searchsorted(v.log.depths, 15.0))
    px, py = xy["pts"][k]
    QTest.mouseClick(v, Qt.LeftButton, Qt.NoModifier, QPoint(int(round(px)), int(round(py)) + 4))
    assert v.pick_depth == pytest.approx(15.0, abs=2.0)
    # dragging along the strip log
    y10, y80 = (st["top"] + d / st["length"] * st["h"] for d in (10.0, 80.0))
    x = int(st["column"].center().x())
    QTest.mousePress(v, Qt.LeftButton, Qt.NoModifier, QPoint(x, int(y10)))
    QTest.mouseMove(v, QPoint(x, int(y80)))
    QTest.mouseRelease(v, Qt.LeftButton, Qt.NoModifier, QPoint(x, int(y80)))
    # (offscreen QTest.mouseMove sends no button state on some platforms: accept press or drag)
    assert v.pick_depth == pytest.approx(80.0, abs=1.0) or v.pick_depth == pytest.approx(10.0, abs=1.0)
    # empty space: nothing changes
    before = v.pick_depth
    box = xz["box"]
    QTest.mouseClick(v, Qt.LeftButton, Qt.NoModifier, QPoint(int(box.left() + 5), int(box.top() + 5)))
    assert v.pick_depth == before
    dlg.close()


def test_drillhole_log_print_has_no_depth_marker(surpac_db):
    """The yellow / orange depth marker is for the screen only: not printed or exported."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QApplication

    from geoviewer.scene.layers import DrillholeLayer
    from geoviewer.ui.drillhole_log import PAPER, DrillholeLogDialog

    app = QApplication.instance() or QApplication([])
    lay = DrillholeLayer(name="surpac", path=None, data=surpac_db)
    dlg = DrillholeLogDialog()
    dlg.show_hole(lay.hole_log("WD033"), 59.6)
    app.processEvents()
    img = dlg.page_image(100)
    accent = QColor(PAPER.accent)
    hits = sum(1 for x in range(0, img.width(), 2) for y in range(0, img.height(), 2)
               if abs(img.pixelColor(x, y).red() - accent.red()) < 25
               and abs(img.pixelColor(x, y).green() - accent.green()) < 25
               and abs(img.pixelColor(x, y).blue() - accent.blue()) < 25)
    assert hits == 0
    assert dlg.view.pick_depth == pytest.approx(59.6)       # still marked on screen
    dlg.close()


def test_azimuth_dip_follow_the_marked_depth(surpac_db):
    """The log header shows the direction at the marked depth: equal to the survey at the
    stations, turning smoothly in between (minimum curvature)."""
    from geoviewer.scene.layers import DrillholeLayer

    lay = DrillholeLayer(name="surpac", path=None, data=surpac_db)
    log = lay.hole_log("WRC001")
    sv = surpac_db.surveys[surpac_db.surveys.hole_id == "WRC001"]
    for d, az, dip in sv[["depth", "azimuth", "dip"]].itertuples(index=False):
        a, p = log.azimuth_dip(d)
        assert a == pytest.approx(az % 360.0, abs=0.05) or abs(a - az) == pytest.approx(360.0, abs=0.05)
        assert p == pytest.approx(dip, abs=0.05)
    a0, _ = log.azimuth_dip(0.0)
    a1, _ = log.azimuth_dip(7.86)
    a2, _ = log.azimuth_dip(10.0)
    assert a0 < a1 < a2                                 # turning between the stations
    tr = surpac_db.trace("WRC001")
    np.testing.assert_allclose(tr.azimuth_dip_at(7.86), log.azimuth_dip(7.86), atol=0.2)


def test_log_header_shows_direction_at_marker(surpac_db):
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QApplication

    import geoviewer.ui.drillhole_log as dl
    from geoviewer.scene.layers import DrillholeLayer

    app = QApplication.instance() or QApplication([])  # noqa: F841
    lay = DrillholeLayer(name="surpac", path=None, data=surpac_db)
    log = lay.hole_log("WRC001")
    texts = []
    real = QPainter.drawText

    def spy(self, *args):
        texts.append(str(args[-1]))
        return real(self, *args)

    QPainter.drawText = spy
    try:
        for depth in (7.86, None):
            img = QImage(1100, 700, QImage.Format_RGB32)
            p = QPainter(img)
            dl.paint_log(p, QRectF(0, 0, 1100, 700), log, dl.SCREEN, depth)
            p.end()
    finally:
        QPainter.drawText = real
    header = [t for t in texts if "azimuth" in t]
    assert any("azimuth 11.8°" in t and "dip 44.8°" in t for t in header)  # dip 0 = down: 90 - 45.2
    assert any("azimuth 0.0°" in t and "dip 45.0°" in t for t in header)
    assert "Collar" in texts and any(t.startswith("Depth 7.86 m") for t in texts)
    assert not any("dip -" in t for t in header)


def test_log_has_no_eoh_labels(surpac_db):
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QApplication

    import geoviewer.ui.drillhole_log as dl
    from geoviewer.scene.layers import DrillholeLayer

    app = QApplication.instance() or QApplication([])  # noqa: F841
    log = DrillholeLayer(name="surpac", path=None, data=surpac_db).hole_log("WD033")
    texts, real = [], QPainter.drawText

    def spy(self, *args):
        texts.append(str(args[-1]))
        return real(self, *args)

    QPainter.drawText = spy
    try:
        for style, depth in ((dl.SCREEN, 50.0), (dl.PAPER, None)):
            img = QImage(1100, 700, QImage.Format_RGB32)
            p = QPainter(img)
            dl.paint_log(p, QRectF(0, 0, 1100, 700), log, style, depth)
            p.end()
    finally:
        QPainter.drawText = real
    assert texts and not any("EOH" in t for t in texts)
    assert sum(t == "120.0 m" for t in texts) == 4          # hole end in XZ and XY, screen + paper


def test_strip_log_zoom_and_pan(surpac_db):
    """Wheel zooms the strip log around the cursor, dragging beside the diagram pans,
    double-click shows the whole hole; picks happen only on the diagram itself."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from geoviewer.scene.layers import DrillholeLayer
    from geoviewer.ui.drillhole_log import DrillholeLogDialog

    app = QApplication.instance() or QApplication([])
    lay = DrillholeLayer(name="surpac", path=None, data=surpac_db)
    dlg = DrillholeLogDialog()
    dlg.resize(1100, 760)
    dlg.show_hole(lay.hole_log("WD033"), 50.0)
    v = dlg.view
    v.grab()
    st = v.layout_info["strip"]
    col = st["column"]

    def wheel(y, notches):
        pos = QPointF(col.center().x(), y)
        ev = QWheelEvent(pos, v.mapToGlobal(pos), QPoint(0, 0), QPoint(0, 120 * notches),
                         Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
        QApplication.sendEvent(v, ev)
        v.grab()

    # zoom in 3 notches around 60 m: 60 m stays under the cursor
    y60 = st["top"] + 60.0 / 120.0 * st["h"]
    wheel(y60, 3)
    st = v.layout_info["strip"]
    assert v.window is not None and st["d1"] - st["d0"] == pytest.approx(120 * 0.8 ** 3, rel=1e-6)
    assert st["d0"] + (y60 - st["top"]) / st["h"] * (st["d1"] - st["d0"]) == pytest.approx(60.0, abs=0.01)
    # a click on the diagram picks the depth of the zoomed scale
    y = int(st["top"] + st["h"] * 0.25)
    expect = st["d0"] + (y - st["top"]) / st["h"] * (st["d1"] - st["d0"])
    QTest.mouseClick(v, Qt.LeftButton, Qt.NoModifier, QPoint(int(col.center().x()), y))
    assert v.pick_depth == pytest.approx(expect, abs=0.2)
    # dragging beside the diagram (on the labels) pans and does not pick
    picked, d0 = v.pick_depth, st["d0"]
    x_lab = int(col.right() + 40)
    QTest.mousePress(v, Qt.LeftButton, Qt.NoModifier, QPoint(x_lab, int(st["top"] + 100)))
    v._pan and v.mouseMoveEvent  # pan started
    assert v._pan is not None
    from PySide6.QtGui import QMouseEvent
    mv = QMouseEvent(QMouseEvent.MouseMove, QPointF(x_lab, st["top"] + 40), v.mapToGlobal(QPointF(x_lab, st["top"] + 40)),
                     Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(v, mv)
    QTest.mouseRelease(v, Qt.LeftButton, Qt.NoModifier, QPoint(x_lab, int(st["top"] + 40)))
    v.grab()
    st2 = v.layout_info["strip"]
    assert st2["d0"] > d0                               # dragged up: deeper part comes into view
    assert st2["d1"] - st2["d0"] == pytest.approx(st["d1"] - st["d0"])
    assert v.pick_depth == picked                       # panning did not pick
    # zooming out past the whole hole, or a double-click: whole hole again
    wheel(y60, -10)
    assert v.window is None
    wheel(y60, 2)
    assert v.window is not None
    QTest.mouseDClick(v, Qt.LeftButton, Qt.NoModifier, QPoint(x_lab, int(st["top"] + 50)))
    assert v.window is None
    # printing always shows the whole hole
    wheel(y60, 3)
    texts = []
    from PySide6.QtGui import QPainter
    real = QPainter.drawText

    def spy(self, *args):
        texts.append(str(args[-1]))
        return real(self, *args)

    QPainter.drawText = spy
    try:
        dlg.page_image(100)
    finally:
        QPainter.drawText = real
    assert not any(t.startswith("showing ") for t in texts)
    dlg.close()



def test_log_header_lines_follow_the_marker(surpac_db):
    """Header: hole (length) / depth and interval / X Y Z / azimuth and dip of the marked point."""
    from geoviewer.scene.layers import DrillholeLayer
    from geoviewer.ui.drillhole_log import header_lines

    log = DrillholeLayer(name="surpac", path=None, data=surpac_db).hole_log("WRC001")
    tr = surpac_db.trace("WRC001")
    a = header_lines(log, 31.9)
    assert a[0] == "Drillhole WRC001  (120.00 m)"
    assert a[1].startswith("Depth 31.90 m") and "QV1 Quartz vein" in a[1] and "(29.00–37.00 m)" in a[1]
    x, y, z = tr.positions_at([31.9])[0]
    assert a[2] == f"X {x:,.3f}     Y {y:,.3f}     Z {z:,.3f}"        # the marked point, not the collar
    assert a[3].startswith("azimuth 47.") and "dip 44.8°" in a[3] and "Length" not in a[3]   # 0 = straight down
    b = header_lines(log, 90.0)
    assert b[2] != a[2] and b[3] != a[3]                              # moves with the marker
    c = header_lines(log, None)                                       # paper: the collar
    cx, cy, cz = tr.collar
    assert c[1] == "Collar" and c[2] == f"X {cx:,.3f}     Y {cy:,.3f}     Z {cz:,.3f}"


def test_desurvey_matches_minimum_curvature_reference(surpac_db):
    """Independent check against the published minimum-curvature formulas (Sawaryn &
    Thorogood, SPE 84246): station positions, the measured direction at every survey depth,
    points between stations on the circular arc of radius dMD / beta."""
    import math

    db = surpac_db
    for hid in db.hole_ids:
        sv = db.surveys[db.surveys.hole_id == hid].sort_values("depth")
        if len(sv) < 2:
            continue
        tr = db.trace(hid)
        md = sv.depth.to_numpy(float)
        inc = np.radians(90.0 + sv.dip.to_numpy(float))       # from vertical down (dip negative down)
        azi = np.radians(sv.azimuth.to_numpy(float))
        if md[0] > 0:
            md, inc, azi = np.r_[0.0, md], np.r_[inc[0], inc], np.r_[azi[0], azi]
        pos = [np.array(tr.collar, float)]
        for k in range(len(md) - 1):
            I1, I2, A1, A2 = inc[k], inc[k + 1], azi[k], azi[k + 1]
            beta = math.acos(min(1.0, max(-1.0, math.cos(I2 - I1)
                                          - math.sin(I1) * math.sin(I2) * (1 - math.cos(A2 - A1)))))
            rf = 1.0 if beta < 1e-12 else 2 / beta * math.tan(beta / 2)
            d = (md[k + 1] - md[k]) / 2 * rf
            pos.append(pos[-1] + d * np.array([math.sin(I1) * math.sin(A1) + math.sin(I2) * math.sin(A2),
                                               math.sin(I1) * math.cos(A1) + math.sin(I2) * math.cos(A2),
                                               -(math.cos(I1) + math.cos(I2))]))
            if beta > 1e-6:                                      # interior points on the arc
                t1 = np.array([math.sin(I1) * math.sin(A1), math.sin(I1) * math.cos(A1), -math.cos(I1)])
                t2 = np.array([math.sin(I2) * math.sin(A2), math.sin(I2) * math.cos(A2), -math.cos(I2)])
                R = (md[k + 1] - md[k]) / beta
                nrm = t2 - (t1 @ t2) * t1
                centre = pos[-2] + R * nrm / np.linalg.norm(nrm)
                X = tr.positions_at(md[k] + np.linspace(0, 1, 11) * (md[k + 1] - md[k]))
                np.testing.assert_allclose(np.linalg.norm(X - centre, axis=1), R, atol=1e-6)
        np.testing.assert_allclose(tr.positions_at(md), np.array(pos), atol=1e-6)
        t_meas = np.column_stack([np.sin(inc) * np.sin(azi), np.sin(inc) * np.cos(azi), -np.cos(inc)])
        np.testing.assert_allclose(tr.direction_at(md), t_meas, atol=1e-9)   # survey honoured


def test_sample_table(surpac_db):
    s = surpac_db.intervals["sample"]
    assert len(s) == 4813
    assert {"gold", "samp_id"} <= set(s.columns)
    assert s.gold.max() == pytest.approx(135.0)


# --- drillholes in the project database (geodatabase.sqlite) ----------------------------------------
def _fill_project_database(surpac_db, folder, sample=True):
    """drillhole_ tables as a user would import them (e.g. from Excel), with their own column names."""
    from geoviewer.io.database import Database

    c = surpac_db.collars[["hole_id", "x", "y", "z", "max_depth"]].rename(
        columns={"hole_id": "BHID", "x": "EAST", "y": "NORTH", "z": "RL", "max_depth": "EOH"})
    survey = surpac_db.surveys.assign(dip=surpac_db.surveys["dip"] + 90.0)   # database: dip 0 = straight down
    tables = {"drillhole_collar": c, "drillhole_survey": survey,
              "drillhole_geology": surpac_db.intervals["geology"],
              "drillhole_translation": surpac_db.translations}
    if sample:
        tables["drillhole_sample"] = surpac_db.intervals["sample"]
    with Database.sqlite(folder / "geodatabase.sqlite") as db:
        for name, df in tables.items():
            db.write_frame(name, df)


def test_drillholes_are_read_from_the_project_database(surpac_db, tmp_path):
    from geoviewer.io.database import project_target
    from geoviewer.io.drillholes import drillhole_signature, project_has_drillholes, read_drillholes_sqlite

    target = project_target(tmp_path)
    assert not project_has_drillholes(target)
    _fill_project_database(surpac_db, tmp_path)
    assert project_has_drillholes(target)
    db = read_drillholes_sqlite(target)
    assert sorted(db.intervals) == ["geology", "sample"]
    pd.testing.assert_frame_equal(db.collars[["hole_id", "x", "y", "z", "max_depth"]],
                                  surpac_db.collars[["hole_id", "x", "y", "z", "max_depth"]], check_dtype=False)
    pd.testing.assert_frame_equal(db.intervals["sample"], surpac_db.intervals["sample"], check_dtype=False)
    assert db.code_descriptions("geology", "rock")["B"] == "Basalt"
    np.testing.assert_allclose(db.trace("WD033").xyz, surpac_db.trace("WD033").xyz)
    sig = {name: (set(cols), rows) for name, cols, rows in drillhole_signature(target)}
    assert sig["drillhole_sample"] == ({"hole_id", "depth_from", "depth_to", "gold", "samp_id"}, 4813)
    assert sig["drillhole_collar"] == ({"BHID", "EAST", "NORTH", "RL", "EOH"}, 94)


def test_database_layer_in_the_workspace(surpac_db, tmp_path):
    import json

    from geoviewer.io import database
    from geoviewer.workspace import Workspace, changed_layers, reload_layers

    database.set_project_folder(tmp_path)
    ws = Workspace(tmp_path)
    layers, errors, _ = ws.load()                            # the set-up makes the (empty) tables
    assert [x.name for x in layers] == ["Drillholes (database)", "Survey (database)"] and not errors   # listed
    layers = [x for x in layers if x.name == "Drillholes (database)"]
    assert layers[0].data.collars.empty and "none yet" in dict(layers[0].info())["Holes"]
    assert ws.new_database_layers() == ([], [])              # shown already
    _fill_project_database(surpac_db, tmp_path, sample=False)
    ((old, lay),), errors = reload_layers([layers[0]])        # e.g. Refresh after an import
    assert not errors and lay.name == "Drillholes (database)" and lay.source == "database"
    assert sorted(lay.data.intervals) == ["geology"]          # the empty assay table is not listed
    lay.table = "geology"
    ws.save([lay], None)
    spec = json.loads(ws.file.read_text())["layers"][0]
    assert spec["source"] == "database" and spec["files"] == []
    # the tables change (another import): Refresh reloads the layer, keeping its settings
    assert changed_layers([lay]).changed == []
    _fill_project_database(surpac_db, tmp_path, sample=True)
    assert changed_layers([lay]).changed == [lay]
    ((old, new),), errs = reload_layers([lay])
    assert not errs and old is lay and sorted(new.data.intervals) == ["geology", "sample"]
    assert new.source == "database"
    # opened again: the layer comes from the workspace file
    again = Workspace(tmp_path)
    layers, errors, _ = again.load()
    assert [x.name for x in layers] == ["Drillholes (database)", "Survey (database)"] and not errors
    # removed by the user: Refresh does not bring it back, the next opening lists it again
    again.save([], None)
    assert again.new_database_layers() == ([], [])
    third = Workspace(tmp_path)
    assert [x.name for x in third.load()[0]] == ["Drillholes (database)", "Survey (database)"]


def test_sqlite_files_are_not_data_files(surpac_db, tmp_path):
    """Drillholes come from the project database: SQLite files are not offered / scanned / opened."""
    import shutil

    from geoviewer.scene.loaders import FILE_FILTER, SUPPORTED_EXTENSIONS, load_paths
    from geoviewer.workspace import list_data_files, loadable_groups

    assert ".sqlite" not in SUPPORTED_EXTENSIONS and ".db" not in SUPPORTED_EXTENSIONS
    assert "sqlite" not in FILE_FILTER and "*.db" not in FILE_FILTER
    _fill_project_database(surpac_db, tmp_path)
    shutil.copy2(tmp_path / "geodatabase.sqlite", tmp_path / "holes.sqlite")
    (tmp_path / "topo.str").write_text("topo,01-Jan-26,,\n0, 0.000, 0.000, 0.000,\n", encoding="utf-8")
    files = list_data_files(tmp_path)
    assert {p.name for p in files} == {"topo.str"}                          # the SQLite files are not listed
    assert [p.name for g in loadable_groups(files, files) for p in g] == ["topo.str"]
    _, errors = load_paths([tmp_path / "holes.sqlite"])
    assert errors and "Unsupported file type" in errors[0]


def test_unreachable_mysql_gives_a_message_not_a_layer(tmp_path):
    from geoviewer.io import database
    from geoviewer.scene.loaders import load_database_drillholes
    from geoviewer.workspace import Workspace

    database.configure(database.DatabaseConfig("mysql", host="127.0.0.1", port=1, user="nobody"))
    layer, errors = load_database_drillholes(tmp_path)
    assert layer is None and "Cannot reach the MySQL server" in errors[0]
    ws = Workspace(tmp_path)
    layers, errors, _ = ws.load()
    assert not errors and "Cannot reach the MySQL server" in ws.database_error
    lay, survey = layers                                      # listed, greyed out, empty
    assert lay.offline and lay.source == "database" and not lay.visible and lay.data.collars.empty
    assert type(survey).__name__ == "SurveyLayer" and survey.offline and survey.data.pits == []


def test_project_database_opened_as_a_file_becomes_the_database_layer(surpac_db, tmp_path):
    """An earlier version could list geodatabase.sqlite as a file layer: it loads as the database layer."""
    from geoviewer.io import database
    from geoviewer.workspace import load_spec

    database.set_project_folder(tmp_path)
    _fill_project_database(surpac_db, tmp_path)
    spec = {"name": "geodatabase", "type": "DrillholeLayer", "part": "", "files": ["geodatabase.sqlite"], "settings": {}}
    layer, errors = load_spec(spec, tmp_path)
    assert not errors and layer.source == "database" and layer.name == "Drillholes (database)"
    assert layer.inputs == [] and len(layer.data.collars) == 94


def test_refresh_does_not_query_an_unchanged_sqlite_database(surpac_db, tmp_path, monkeypatch):
    import geoviewer.workspace as wsmod
    from geoviewer.io import database
    from geoviewer.scene.loaders import load_database_drillholes
    from geoviewer.scene.production import MinedShape, ProductionLog

    database.set_project_folder(tmp_path)
    _fill_project_database(surpac_db, tmp_path, sample=False)
    layer, _ = load_database_drillholes(tmp_path)
    calls = []
    real = wsmod.database_signature
    monkeypatch.setattr(wsmod, "database_signature", lambda k, t: calls.append(1) or real(k, t))
    assert wsmod.changed_layers([layer]).changed == [] and calls == []        # file unchanged: no query
    ProductionLog(tmp_path).put(MinedShape(1, "2026-01-01", "a", "xy", [[0, 0], [1, 0], [1, 1]], 0, 1))
    assert wsmod.changed_layers([layer]).changed == [] and len(calls) == 1   # file changed, drillholes not
    assert wsmod.changed_layers([layer]).changed == [] and len(calls) == 1   # remembered: no query again


def test_database_dip_is_measured_from_straight_down(surpac_db, tmp_path):
    """drillhole_survey dips: 0 = straight down, 90 = horizontal; paths match the Surpac data."""
    from geoviewer.io.database import project_target
    from geoviewer.io.drillholes import display_dip, read_drillholes_sqlite

    _fill_project_database(surpac_db, tmp_path, sample=False)
    db = read_drillholes_sqlite(project_target(tmp_path))
    assert db.dip_zero_down and not db.dip_positive_down
    assert db.surveys["dip"].between(0, 90).all()                 # e.g. 0 vertical, 30 for Surpac -60
    for hid in ("WD004", "WD033", "WRC001"):
        np.testing.assert_allclose(db.trace(hid).xyz, surpac_db.trace(hid).xyz, atol=1e-9)
    # a hand-made hole: dip 0 goes straight down, dip 90 goes level
    collar = pd.DataFrame({"hole_id": ["V", "H"], "x": [0.0, 0.0], "y": [0.0, 0.0], "z": [100.0, 100.0],
                           "max_depth": [50.0, 50.0]})
    survey = pd.DataFrame({"hole_id": ["V", "H"], "depth": [0.0, 0.0], "azimuth": [0.0, 90.0], "dip": [0.0, 90.0]})
    from geoviewer.io.drillholes import DrillholeDB

    hand = DrillholeDB(collar, survey, {}, dip_zero_down=True)
    np.testing.assert_allclose(hand.trace("V").positions_at([50.0])[0], [0, 0, 50.0], atol=1e-9)
    np.testing.assert_allclose(hand.trace("H").positions_at([50.0])[0], [50.0, 0, 100.0], atol=1e-9)
    assert display_dip(hand.trace("V").azimuth_dip_at(10.0)[1]) == pytest.approx(0.0)
    assert display_dip(hand.trace("H").azimuth_dip_at(10.0)[1]) == pytest.approx(90.0)
    assert display_dip(-60.0) == 30.0 and str(display_dip(-90.0)) == "0.0"


def test_log_grade_graphs_one_per_field_up_to_two(surpac_db):
    """Beside the strip log: a graph per numeric field (at most two), a combobox above each to
    pick the field; Surpac's -1 (below detection) is drawn as 0, -2 (not analysed) is a gap."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from geoviewer.scene.layers import DrillholeLayer
    from geoviewer.ui.drillhole_log import DrillholeLogDialog, grade_at, grade_text, graph_values

    app = QApplication.instance() or QApplication([])  # noqa: F841
    assert graph_values([(0, 1, 2.5), (1, 2, -1.0), (2, 3, -2.0), (3, 4, 0.7)]) == [(0, 1, 2.5), (1, 2, 0.0),
                                                                                   (3, 4, 0.7)]
    assert grade_text(-1.0) == "below detection" and grade_text(-2.0) == "not analysed" and grade_text(None) == "–"
    lay = DrillholeLayer(name="surpac", path=None, data=surpac_db)
    assert [name for name, _t, _c in lay.grade_fields()] == ["gold"]          # the demo holes have one grade
    log = lay.hole_log("WD033")
    rows = log.grades["gold"]
    assert rows and rows == sorted(rows) and all(b > a for a, b, _v in rows)
    dlg = DrillholeLogDialog()
    dlg.resize(1100, 760)
    dlg.show_hole(log, None)
    v = dlg.view
    v.grab()
    st = v.layout_info["strip"]
    assert v.grades == ["gold"] and len(st["graphs"]) == 1                    # one field: one graph
    assert not v.combos[0].isHidden() and v.combos[1].isHidden()
    g = st["graphs"][0]
    assert g.top() == st["column"].top() and g.height() == st["column"].height()   # as high as the log
    assert g.left() > st["column"].right()
    # a click on the graph marks that depth; the header shows the value there
    y = st["top"] + 40.0 / st["length"] * st["h"]
    QTest.mouseClick(v, Qt.LeftButton, Qt.NoModifier, QPoint(int(g.center().x()), int(round(y))))
    assert v.pick_depth == pytest.approx(40.0, abs=0.5)
    assert f"gold {grade_text(grade_at(log, 'gold', v.pick_depth))}" in dlg.head_depth.text()
    # two fields (or more): two graphs, the choice kept for the next hole
    log.grades = {"gold": rows, "silver": [(a, b, x * 3) for a, b, x in rows], "copper": rows}
    dlg.show_hole(log, None)
    v.grab()
    assert v.grades == ["gold", "silver"] and len(v.layout_info["strip"]["graphs"]) == 2
    assert not v.combos[1].isHidden() and v.combos[1].count() == 3
    v.combos[1].setCurrentText("copper")
    assert v.grades == ["gold", "copper"]
    other = lay.hole_log("WRC094")
    other.grades = dict(log.grades)
    dlg.show_hole(other, None)
    assert v.grades == ["gold", "copper"]                                      # kept
    other.grades = {}
    dlg.show_hole(other, None)                                                 # no fields: no graph
    v.grab()
    assert v.grades == [] and v.layout_info["strip"]["graphs"] == [] and all(c.isHidden() for c in v.combos)
    # on paper the field names are written above the graphs
    log.grades = {"gold": rows}
    dlg.show_hole(log, None)
    assert not dlg.page_image(60).isNull()
    dlg.close()


def test_grade_axis_ticks_are_round_and_end_on_the_highest_value():
    from geoviewer.ui.drillhole_log import grade_ticks

    assert grade_ticks(135.0) == (150.0, [0.0, 50.0, 100.0, 150.0])           # not 0-200: little waste
    assert grade_ticks(473.8) == (500.0, [0.0, 250.0, 500.0])
    assert grade_ticks(8.3) == (10.0, [0.0, 5.0, 10.0])
    end, ticks = grade_ticks(0.87)
    assert ticks[0] == 0.0 and ticks[-1] == end >= 0.87 and 2 <= len(ticks) <= 5
    assert all(abs(b - a - (ticks[1] - ticks[0])) < 1e-12 for a, b in zip(ticks, ticks[1:]))   # even steps
    assert grade_ticks(0.0) == (1.0, [0.0])                                    # nothing above 0


def test_scale_bar_goes_to_a_corner_clear_of_the_hole():
    """The scale bar of a projection pane: bottom left unless the hole path (or a label / the
    north arrow) is there, then the next free corner; a shorter bar when no corner is free."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import numpy as np
    from PySide6.QtCore import QRectF
    from PySide6.QtWidgets import QApplication

    from geoviewer.ui.drillhole_log import SCREEN, scale_bar_place

    app = QApplication.instance() or QApplication([])  # noqa: F841
    box = QRectF(100, 50, 600, 400)
    mpp = 0.5
    up_right = np.array([[650.0, 80.0], [500.0, 250.0]])                       # nowhere near the bottom left
    assert scale_bar_place(box, mpp, SCREEN, up_right)["corner"] == "bottom-left"
    to_bottom_left = np.array([[650.0, 80.0], [110.0, 445.0]])                 # a long leg into that corner
    g = scale_bar_place(box, mpp, SCREEN, to_bottom_left)
    assert g["corner"] == "bottom-right" and box.contains(g["frame"])
    # bottom left and right taken by the path, top right by the north arrow: top left
    v = np.array([[110.0, 445.0], [400.0, 300.0], [690.0, 445.0]])
    arrow = [QRectF(box.right() - 30, box.top(), 30, 50)]
    assert scale_bar_place(box, mpp, SCREEN, v, arrow)["corner"] == "top-left"
    # the hole everywhere: still placed, inside the pane
    everywhere = np.array([[x, y] for y in np.linspace(55, 445, 40) for x in (105, 695)])
    g = scale_bar_place(box, mpp, SCREEN, everywhere)
    assert g["corner"] in ("bottom-left", "bottom-right", "top-left", "top-right") and box.contains(g["frame"])


def test_drillholes_in_a_section_are_thick_lines_from_the_corridor(surpac_db):
    """A hole drawn as a cylinder (diameter 4 m) shows in a section as thick lines along its path, also when
    the section passes beside it (within the corridor), with its name and the interval legend."""
    import pyvista as pv

    from geoviewer.scene.layers import DrillholeLayer
    from geoviewer.scene.scene import Scene
    from geoviewer.scene.section import SectionLine, build_section

    sc = Scene(pv.Plotter(off_screen=True))
    lay = DrillholeLayer(name="surpac", path=None, data=surpac_db)
    assert lay.diameter > 0                                        # the default: cylinders in 3D
    sc.add_layer(lay)
    c = surpac_db.trace("WD033").collar - sc.origin
    for offset in (0.0, 5.0):                                      # on the hole's plane, and 5 m beside it
        y = c[1] + offset
        line = SectionLine(np.array([[c[0] - 50.0, y, 0.0], [c[0] + 150.0, y, 0.0]]), axis=2)
        res = build_section(sc, line, 10.0)
        assert "WD033" in [text for _u, _v, _d, text in res.labels]
        assert res.legends                                         # the rock codes cut
        drawn = [a for a in res.actors if a.GetMapper().GetInput().GetNumberOfLines() > 0]
        assert drawn and all(a.GetMapper().GetInput().GetNumberOfPolys() == 0 for a in drawn)
        assert max(a.GetProperty().GetLineWidth() for a in drawn) >= 7.0
    sc.plotter.close()


def test_the_corridor_decides_which_holes_a_section_shows(surpac_db):
    """A hole 30 m beside the section line: left out with a 10 m corridor, drawn with a 65 m one (whatever
    the hole's diameter in 3D)."""
    import pyvista as pv

    from geoviewer.scene.layers import DrillholeLayer
    from geoviewer.scene.scene import Scene
    from geoviewer.scene.section import SectionLine, build_section

    for diameter in (0.0, 1.5):
        sc = Scene(pv.Plotter(off_screen=True))
        lay = DrillholeLayer(name="surpac", path=None, data=surpac_db)
        lay.diameter = diameter
        sc.add_layer(lay)
        tr = surpac_db.trace("WD033")
        p = tr.path(step=2.0) - sc.origin                         # the hole runs east (azimuth 90)
        y = float(p[:, 1].mean()) + 30.0
        line = SectionLine(np.array([[p[:, 0].min() - 20, y, 0.0], [p[:, 0].max() + 20, y, 0.0]]), axis=2)

        def shown(hw):
            res = build_section(sc, line, hw)
            return "WD033" in [text for _u, _v, _d, text in res.labels]

        assert not shown(10.0) and shown(65.0), diameter
        sc.plotter.close()
