"""Block model management: the encrypted .gvbm file, block models inside a solid, Surpac
ASCII import, attributes, samples, the neighbour search, variograms, estimation and its
validation - and the manager in the main window."""

from __future__ import annotations

import os
import shutil
import time

import numpy as np
import pytest

from geoviewer.blockmodel import attributes as battr
from geoviewer.blockmodel import store
from geoviewer.blockmodel.build import blocks_in_solid, mesh_volume, read_surpac_ascii
from geoviewer.blockmodel.estimate import EstimationParams, cross_validate, estimate, suggest_search
from geoviewer.blockmodel.samples import Samples, drillhole_samples, string_samples, suggest_top_cut
from geoviewer.blockmodel.search import Anisotropy, Neighbours
from geoviewer.blockmodel.variogram import Experimental, Structure, VariogramModel, fit, suggest_model
from geoviewer.blockmodel import validate as bval
from geoviewer.io.block_model import read_block_csv


@pytest.fixture(scope="module")
def ore(demo):
    from geoviewer.io import read_dtm
    from geoviewer.scene.solid import closed_mesh

    return closed_mesh(read_dtm(demo / "ore1.dtm"))


@pytest.fixture(scope="module")
def gold(demo, ore):
    from geoviewer.io.drillholes import read_drillholes_sqlite
    from geoviewer.scene.solid import points_inside

    db = read_drillholes_sqlite(demo / "geodatabase.sqlite")
    s = drillhole_samples(db, "assay", "gold", 2.0)
    return s.subset(points_inside(s.xyz, *ore))


# --- the file ---------------------------------------------------------------------------------
def test_gvbm_round_trip_is_exact_and_unreadable(demo, tmp_path):
    bm = read_block_csv(demo / "block_model.csv", use_cache=False)
    bm.info = {"name": "demo", "descriptions": {"gold": "Au g/t"}}
    p = store.save_block_model(bm, tmp_path / "demo.gvbm")
    raw = p.read_bytes()
    assert raw[:4] == store.MAGIC and b"gold" not in raw and b"classification" not in raw
    assert len(raw) < (demo / "block_model.csv").stat().st_size / 5          # compressed
    back = store.read_block_model(p)
    np.testing.assert_array_equal(back.centroids, bm.centroids)
    np.testing.assert_array_equal(back.sizes, bm.sizes)
    for n in bm.attributes:
        np.testing.assert_array_equal(back.attributes[n], bm.attributes[n])
    assert back.info["descriptions"]["gold"] == "Au g/t" and back.info["name"] == "demo"
    assert not list(tmp_path.glob("*.tmp"))


def test_gvbm_rejects_changed_or_cut_files(demo, tmp_path):
    bm = read_block_csv(demo / "block_model.csv", use_cache=False)
    p = store.save_block_model(bm, tmp_path / "a.gvbm")
    raw = bytearray(p.read_bytes())
    for pos in (10, 60, len(raw) // 2, len(raw) - 3):       # header, first piece, middle, tag
        bad = bytearray(raw)
        bad[pos] ^= 0x01
        q = tmp_path / f"bad{pos}.gvbm"
        q.write_bytes(bytes(bad))
        with pytest.raises(store.BlockFileError):
            store.read_block_model(q)
    q = tmp_path / "cut.gvbm"
    q.write_bytes(bytes(raw[:-40]))
    with pytest.raises(store.BlockFileError):
        store.read_block_model(q)
    q = tmp_path / "text.gvbm"
    q.write_text("X,Y,Z\n")
    with pytest.raises(store.BlockFileError):
        store.read_block_model(q)


def test_large_model_spans_several_pieces(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "CHUNK", 1 << 12)              # many small pieces
    n = 20000
    rng = np.random.default_rng(3)
    ijk = rng.integers(0, 200, (n, 3))
    from geoviewer.io.block_model import BlockModel

    bm = BlockModel(None, ijk * 5.0 + 2.5, np.full((n, 3), 5.0, dtype=np.float32),
                    {"au": rng.random(n).astype(np.float32)})
    p = store.save_block_model(bm, tmp_path / "big.gvbm")
    back = store.read_block_model(p)
    np.testing.assert_array_equal(back.attributes["au"], bm.attributes["au"])
    np.testing.assert_allclose(back.centroids, bm.centroids)


# --- building -----------------------------------------------------------------------------------
@pytest.mark.parametrize("sub", [1, 2, 4])
def test_blocks_inside_solid(ore, sub):
    from geoviewer.scene.solid import points_inside

    bm = blocks_in_solid(*ore, (10, 10, 5), sub=sub, attributes=[("gold", None, "Au"), ("sg", 2.7, "")])
    vol = float(np.prod(bm.sizes.astype(np.float64), axis=1).sum())
    solid = mesh_volume(*ore)
    assert abs(vol - solid) / solid < (0.02 if sub == 1 else 0.01)
    if sub == 1:
        assert points_inside(bm.centroids, *ore).all()
    assert (bm.attributes["sg"] == np.float32(2.7)).all() and (bm.attributes["gold"] == -99).all()
    g = bm.grid()                                    # aligned; sub-blocks never overlap
    occ = np.zeros(g.shape, dtype=np.uint8)
    for (i, j, k), (a, b, c) in zip(g.ijk_min, g.span):
        occ[i:i + a, j:j + b, k:k + c] += 1
    assert occ.max() == 1
    assert len(bm.block_sizes()) == (1 if sub == 1 else 2)


def test_origin_is_kept_on_whole_blocks(ore):
    bm = blocks_in_solid(*ore, (10, 10, 10), origin=(3.0, 7.0, 1.0))
    lo = bm.centroids - bm.sizes / 2
    np.testing.assert_allclose(np.mod(lo - [3.0, 7.0, 1.0], 10.0) % 10.0, 0, atol=1e-6)


def test_surpac_ascii_import_with_header_lines_and_spaces(tmp_path, demo):
    p = tmp_path / "export.txt"
    p.write_text("Block model export from Surpac\nOrigin 0 0 0\n\n"
                 "X Y Z size(X) size(Y) size(Z) au rock\n"
                 "5 5 5 10 10 10 1.5 OX\n15 5 5 10 10 10 -99 FR\n", encoding="latin-1")
    bm = read_surpac_ascii(p)
    assert bm.n_blocks == 2 and bm.attributes["au"][0] == np.float32(1.5)
    assert bm.is_categorical("rock")
    csv = read_surpac_ascii(demo / "block_model.csv")
    assert csv.n_blocks == 60635 and set(csv.attribute_names) == {"classification_no", "gold", "sg"}
    assert csv.info["history"]


def test_attribute_operations(ore):
    bm = blocks_in_solid(*ore, (20, 20, 10), attributes=[("gold", None, "")])
    battr.add_attribute(bm, "cu", 0.5, "copper %")
    with pytest.raises(ValueError):
        battr.add_attribute(bm, "CU")                      # same name, other case
    with pytest.raises(ValueError):
        battr.add_attribute(bm, "status")                  # the reserve's attribute
    with pytest.raises(ValueError):
        battr.add_attribute(bm, "2nd")
    battr.rename_attribute(bm, "cu", "copper")
    assert "cu" not in bm.attributes and battr.descriptions(bm)["copper"] == "copper %"
    m = np.zeros(bm.n_blocks, dtype=bool)
    m[:10] = True
    assert battr.fill_attribute(bm, "copper", 2.0, m) == 10
    assert battr.attribute_stats(bm, "copper")["max"] == 2.0
    battr.fill_attribute(bm, "copper", None)
    assert battr.attribute_stats(bm, "copper")["n"] == 0
    battr.remove_attribute(bm, "copper")
    assert list(bm.attributes) == ["gold"]
    assert len(bm.info["history"]) >= 4


# --- samples ----------------------------------------------------------------------------------
def test_compositing_is_length_weighted():
    import pandas as pd

    from geoviewer.io.drillholes import DrillholeDB

    collars = pd.DataFrame({"hole_id": ["H1"], "x": [0.0], "y": [0.0], "z": [100.0], "max_depth": [10.0]})
    surveys = pd.DataFrame({"hole_id": ["H1"], "depth": [0.0], "azimuth": [0.0], "dip": [-90.0]})
    assay = pd.DataFrame({"hole_id": ["H1"] * 4, "depth_from": [0.0, 1.0, 2.0, 3.5],
                          "depth_to": [1.0, 2.0, 3.5, 4.0], "au": [1.0, 3.0, -1.0, 4.0]})
    db = DrillholeDB(collars, surveys, {"assay": assay})
    s = drillhole_samples(db, "assay", "au", composite=2.0, min_cover=0.5)
    # 0-2 m: (1 + 3) / 2; 2-4 m: only 3.5-4 is assayed (-1 = missing): 0.5 m < 50 % cover -> dropped
    np.testing.assert_allclose(s.value, [2.0])
    np.testing.assert_allclose(s.xyz[0], [0, 0, 99.0], atol=1e-9)
    s2 = drillhole_samples(db, "assay", "au", composite=None)
    np.testing.assert_allclose(s2.value, [1.0, 3.0, 4.0])


def test_string_samples(demo):
    from geoviewer.io import read_str

    sf = read_str(demo / "topo1.str")
    with pytest.raises(ValueError):
        string_samples(sf, 9)          # no numbers in a field that does not exist


def test_top_cut_only_for_skewed_data():
    rng = np.random.default_rng(0)
    assert suggest_top_cut(rng.normal(10, 1, 500)) is None
    v = rng.lognormal(0, 1.6, 2000)
    v[:3] = v.max() * 5
    cut = suggest_top_cut(v)
    assert cut is not None and np.isclose(cut, np.percentile(v, 99))


# --- search, variogram, estimation ---------------------------------------------------------------
def test_neighbours_match_brute_force():
    rng = np.random.default_rng(1)
    P = rng.uniform(0, 300, (1500, 3))
    Q = rng.uniform(0, 300, (300, 3))
    g = rng.integers(0, 40, 1500)
    idx, d = Neighbours(P, g).query(Q, 12, 50.0)
    D = np.sqrt(((Q[:, None] - P[None]) ** 2).sum(-1))
    for i in range(len(Q)):
        o = np.argsort(D[i])
        o = o[D[i][o] < 50][:12]
        np.testing.assert_array_equal(idx[i][idx[i] >= 0], o)
    idx2, _ = Neighbours(P, g).query(Q, 12, 50.0, max_per_group=2)
    for row in idx2:
        r = row[row >= 0]
        if len(r):
            assert np.bincount(g[r]).max() <= 2
    idx3, _ = Neighbours(P, g).query(Q, 12, 50.0, exclude=g[:300])
    for i, row in enumerate(idx3):
        assert not np.any(g[row[row >= 0]] == g[i])


def test_anisotropy_axes_are_orthonormal_and_scaled():
    a = Anisotropy(bearing=60, plunge=15, dip=30, ratio1=0.5, ratio2=0.25)
    A = a.axes()
    np.testing.assert_allclose(A @ A.T, np.eye(3), atol=1e-12)
    np.testing.assert_allclose(A[0], [np.sin(np.radians(60)) * np.cos(np.radians(15)),
                                      np.cos(np.radians(60)) * np.cos(np.radians(15)), -np.sin(np.radians(15))])
    np.testing.assert_allclose(np.linalg.norm(a.transform(A[1] * 5)), 10)    # semi axis: half the range
    np.testing.assert_allclose(np.linalg.norm(a.transform(A[2] * 5)), 20)


@pytest.mark.parametrize("kind", ["spherical", "exponential", "gaussian"])
def test_fit_recovers_a_known_model(kind):
    true = VariogramModel(0.2, [Structure(kind, 1.0, 80.0)])
    h = np.arange(1, 21) * 10.0
    e = Experimental(h, true.gamma(h), np.full(len(h), 1000), 10.0)
    m, _err = fit(e, nested=False)
    assert m.structures[0].kind == kind
    assert abs(m.nugget - 0.2) < 0.03 and abs(m.structures[0].sill - 1.0) < 0.05
    assert abs(m.structures[0].range - 80) / 80 < 0.06


def test_kriging_honours_data_and_weights_sum_to_one():
    xyz = np.array([[0, 0, 0], [10, 0, 0], [0, 10, 0], [10, 10, 5.0]])
    S = Samples(xyz, np.array([1.0, 2.0, 3.0, 4.0]), np.full(4, -1), np.ones(4))
    m = VariogramModel(0.0, [Structure("spherical", 1.0, 50.0)])
    p = EstimationParams("ok", model=m, radius=100, min_samples=1, max_samples=8, discretisation=(1, 1, 1))
    r = estimate(xyz, np.ones((4, 3)), S, p, ("value", "var", "slope", "ke"))
    np.testing.assert_allclose(r["value"], S.value, atol=1e-4)            # exact at the samples
    assert np.all(r["var"] < 1e-4) and np.allclose(r["slope"], 1, atol=1e-3)
    const = Samples(xyz, np.full(4, 7.0), np.full(4, -1), np.ones(4))
    q = np.array([[5, 5, 2.0], [30, 30, 0.0]])
    for meth in ("ok", "sk", "idw", "nn"):
        p.method = meth
        np.testing.assert_allclose(estimate(q, np.full((2, 3), 5.0), const, p)["value"], 7.0)


def test_search_radius_min_samples_and_passes():
    xyz = np.array([[0, 0, 0], [100.0, 0, 0]])
    S = Samples(xyz, np.array([1.0, 3.0]), np.array([0, 1]), np.ones(2))
    p = EstimationParams("idw", radius=30, min_samples=1, max_samples=4)
    q = np.array([[10.0, 0, 0], [50.0, 0, 0]])
    r = estimate(q, np.ones((2, 3)), S, p, ("value", "pass", "ns"))
    assert r["value"][0] == 1.0 and np.isnan(r["value"][1])
    p.passes, p.pass_factor = 2, 2.0
    r = estimate(q, np.ones((2, 3)), S, p, ("value", "pass", "ns"))
    assert r["pass"].tolist() == [1, 2] and r["value"][1] == pytest.approx(2.0) and r["ns"][1] == 2
    p.passes, p.min_samples = 1, 2                     # only one sample within 30 m of the first block
    assert np.isnan(estimate(q[:1], np.ones((1, 3)), S, p)["value"][0])


def test_demo_gold_suggestions_and_estimate(gold, ore):
    s = gold
    s.value = np.minimum(s.value, 18.0)
    sug = suggest_model(s.xyz, s.value, s.hole, 2.0)
    m = sug.model
    assert m.nugget >= 0 and all(st.sill > 0 and st.range > 0 for st in m.structures)
    assert 0.0 < m.anisotropy.ratio2 <= m.anisotropy.ratio1 <= 1.0
    assert sug.notes and sug.omni.pairs.sum() > 0
    p = EstimationParams("ok", model=m, search=m.anisotropy, radius=m.range, max_samples=16, max_per_hole=0)
    ss = suggest_search(s, p, n_test=150)
    assert ss.min_samples <= ss.max_samples and ss.radius > 0 and np.isfinite(ss.score["rmse"])
    p.radius, p.min_samples, p.max_samples, p.max_per_hole = ss.radius, ss.min_samples, ss.max_samples, ss.max_per_hole
    cv = cross_validate(s, p, n_test=150)
    assert cv["cover"] > 0.9
    bm = blocks_in_solid(*ore, (20, 20, 10))
    r = estimate(bm.centroids, bm.sizes, s, p, ("value", "var", "slope"))
    v = r["value"][np.isfinite(r["value"])]
    assert len(v) > 0.9 * bm.n_blocks
    assert abs(v.mean() - s.value.mean()) / s.value.mean() < 0.35        # declustering moves it, not far
    # validation
    cvr = bval.cross_validation(s, p, n_test=300)
    nn = bval.nn_reference(bm.centroids, bm.sizes, s, p)
    vol = np.prod(bm.sizes.astype(float), axis=1)
    g = bval.global_check(r["value"], nn, vol, s)
    assert np.isfinite(g.bias_pct) and g.cv_est < g.cv_nn                  # kriging smooths
    sw = [bval.swath(a, bm.centroids, r["value"], nn, vol, s) for a in range(3)]
    assert all(len(x.centre) > 2 for x in sw)
    found = bval.findings(cvr, g, sw)
    assert found and all(lv in ("ok", "warn", "bad", "info") for lv, _t in found)


# --- the manager in the main window ----------------------------------------------------------
def _wait(app, cond, timeout=60):
    t0 = time.time()
    while not cond() and time.time() - t0 < timeout:
        app.processEvents()
        time.sleep(0.02)
    return cond()


def test_block_model_manager_workflow(demo, tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

    import geoviewer.ui.main_window as mw
    from geoviewer.scene.layers import BlockModelLayer
    from geoviewer.ui.estimation_dialog import EstimationDialog

    app = QApplication.instance() or QApplication([])
    (tmp_path / "s.ini").write_text("[database]\nbackend=sqlite\n[workspace]\nrestore_last=false\n",
                                    encoding="utf-8")
    monkeypatch.setattr(mw, "QSettings", lambda *a: QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat))
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: pytest.fail(f"warning: {a[1:]}"))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    ws = tmp_path / "ws"
    ws.mkdir()
    for f in ("ore1.dtm", "ore1.str", "geodatabase.sqlite"):
        shutil.copy2(demo / f, ws / f)
    out = ws / "ore1_bm.gvbm"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    win = mw.MainWindow()
    win.quiet_errors = True
    try:
        win.open_workspace(ws, confirm=False)
        assert _wait(app, lambda: not win.loads_pending and not win._threads and win.workspace is not None
                     and any(lay.name == "ore1" for lay in win.scene.layers))
        win.open_block_manager()
        mgr = win.block_manager
        assert mgr is not None and mgr.bm is None
        from geoviewer.ui.block_manager import NewFromSolidDialog

        dlg = NewFromSolidDialog(win.scene, ws, mgr)
        assert dlg.solid.currentText() == "ore1"
        dlg.sx.setValue(20)
        dlg.sy.setValue(20)
        dlg.sz.setValue(10)
        dlg._create()
        assert dlg.result_model is not None and out.exists()
        mgr._adopt(dlg.result_model, dlg.out_path)
        assert mgr.bm.n_blocks > 100 and {"gold", "sg"} <= set(mgr.bm.attributes)

        # estimate gold from the database drillholes inside ore1
        est = EstimationDialog(mgr.bm, win, "gold", mgr)
        i = next(k for k in range(est.source.count()) if est.source.itemData(k)[0] == "dh")
        est.source.setCurrentIndex(i)
        est.table_box.setCurrentText("assay")
        est.field_box.setCurrentText("gold")
        est.domain.setCurrentIndex(est.domain.findData("ore1"))
        assert est.load_samples()
        assert est.block_where.currentData() == "ore1"
        est.suggest_variogram()
        assert est.var_suggestion is not None
        # the search ellipsoid (Estimate tab) follows the variogram anisotropy while "same" is ticked
        fit = est.var_suggestion.model.anisotropy
        assert est.sa[3].value() == pytest.approx(fit.ratio1, abs=0.01)
        est.va[3].setValue(0.5)
        est.va[4].setValue(0.4)
        assert (est.sa[3].value(), est.sa[4].value()) == (0.5, 0.4)
        est.same_ellipsoid.setChecked(False)               # its own ellipsoid: no longer follows
        est.va[3].setValue(0.6)
        assert est.sa[3].value() == 0.5
        est.same_ellipsoid.setChecked(True)
        assert est.sa[3].value() == 0.6
        est.set_model(est.var_suggestion.model)
        assert est.sa[3].value() == pytest.approx(fit.ratio1, abs=0.01)
        est.suggest_search()
        est.run_estimation()
        assert est.written == "gold"
        assert {"gold_ns", "gold_kv", "gold_sor"} <= set(mgr.bm.attributes)
        assert mgr.bm.valid_mask("gold").sum() > 0.5 * mgr.bm.n_blocks
        assert est.vfindings.texts()                                       # validation ran
        assert "gold" in mgr.bm.info["validations"]
        mgr._changed("gold")
        assert mgr.save()
        rec = mgr.bm.info["estimations"]["gold"]

        # the layer shows the saved model
        assert _wait(app, lambda: any(isinstance(lay, BlockModelLayer) and lay.path == out
                                      and "gold_ns" in lay.data.attributes for lay in win.scene.layers), 30)
        back = store.read_block_model(out)
        assert back.info["estimations"]["gold"]["params"] == rec["params"]
        # the dialog opens again with the same settings and can validate at once
        again = EstimationDialog(back, win, "gold", mgr, validate_only=True)
        assert again.samples is not None and again.tabs.currentIndex() == 3
        assert again.method.currentData() == rec["params"]["method"]
        assert again.radius.value() == pytest.approx(rec["params"]["radius"], rel=1e-3)
        again.deleteLater()
        est.deleteLater()

        # Close (button / X / Esc): asks about unsaved changes; Cancel keeps the manager open
        assert mgr.isVisible()
        mgr.dirty = True
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Cancel)
        mgr.close()
        assert mgr.isVisible()
        mgr.reject()
        assert mgr.isVisible()
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Discard)
        mgr.close()
        assert not mgr.isVisible()
        win.open_block_manager()                        # opens again
        assert win.block_manager.isVisible()
        mgr.dirty = False
        win.block_manager.reject()                      # Esc, nothing unsaved: closes at once
        assert not win.block_manager.isVisible()
    finally:
        if getattr(win, "block_manager", None) is not None:
            win.block_manager.dirty = False
        win.close()
        win.deleteLater()
        app.processEvents()



def test_opened_block_csv_is_imported_in_the_manager(demo, tmp_path, monkeypatch):
    """Opening a block model CSV imports it into a .gvbm in the Block Model Manager (never shown
    as CSV), then offers to delete the CSV; the .gvbm shows as the layer."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QFile, QSettings
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

    import geoviewer.ui.main_window as mw
    from geoviewer.scene.layers import BlockModelLayer
    from geoviewer.ui.block_manager import ImportAsciiDialog

    app = QApplication.instance() or QApplication([])
    (tmp_path / "s.ini").write_text("[database]\nbackend=sqlite\n[workspace]\nrestore_last=false\n",
                                    encoding="utf-8")
    monkeypatch.setattr(mw, "QSettings", lambda *a: QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat))
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: pytest.fail(f"warning: {a[1:]}"))
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[1]) or QMessageBox.Yes)
    monkeypatch.setattr(QFile, "moveToTrash", lambda *a: (False, ""))   # not into the user's Recycle Bin
    ws = tmp_path / "ws"
    ws.mkdir()
    csv = ws / "block_model.csv"
    shutil.copy2(demo / "block_model.csv", csv)
    out = ws / "block_model.gvbm"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    offered = []

    def run_import(dlg):
        offered.append(dlg.source)
        dlg._import()
        return dlg.result()

    monkeypatch.setattr(ImportAsciiDialog, "exec", run_import)
    win = mw.MainWindow()
    try:
        win.open_paths([str(csv)])                   # no workspace yet: the CSV's folder becomes one
        assert _wait(app, lambda: not win._import_queue and not win._importing and out.exists(), 60)
        assert offered == [csv]
        assert win.block_manager is not None and win.block_manager.path == out
        assert asked == ["Remove the original file?"] and not csv.exists()     # deleted when confirmed
        assert _wait(app, lambda: any(isinstance(lay, BlockModelLayer) and lay.path == out
                                      for lay in win.scene.layers), 30)
        assert not any(isinstance(lay, BlockModelLayer) and str(lay.path).endswith(".csv")
                       for lay in win.scene.layers)
        assert store.read_block_model(out).n_blocks == read_surpac_ascii(demo / "block_model.csv").n_blocks
    finally:
        if getattr(win, "block_manager", None) is not None:
            win.block_manager.dirty = False
        win.close()
        win.deleteLater()
        app.processEvents()

# --- worker processes (the window keeps responding) -----------------------------------------
def test_parallel_estimate_matches_and_reports_progress(gold, ore, monkeypatch):
    import threading

    from geoviewer.blockmodel import parallel

    monkeypatch.setattr(parallel, "SMALL_ESTIMATE", 0)         # use the worker processes even for a few blocks
    s = gold
    m = VariogramModel(0.5, [Structure("spherical", 5.0, 150.0)], Anisotropy(30, 0, 0, 0.6, 0.3))
    p = EstimationParams("ok", model=m, search=m.anisotropy, radius=300, min_samples=4, max_samples=24,
                         max_per_hole=4, passes=2)
    bm = blocks_in_solid(*ore, (20, 20, 10))
    seen = []
    stalls = []
    stop = threading.Event()

    def ticker():                                              # stands in for the GUI thread
        last = time.perf_counter()
        while not stop.is_set():
            time.sleep(0.01)
            now = time.perf_counter()
            stalls.append(now - last)
            last = now

    th = threading.Thread(target=ticker, daemon=True)
    th.start()
    try:
        r = parallel.estimate(bm.centroids, bm.sizes, s, p, ("value", "var", "ns"),
                              progress=lambda f, text: seen.append((f, text)))
        cv = parallel.run(bval.cross_validation, s, p, text="cv")
        ss = parallel.suggest_search(s, p, n_test=100)
    finally:
        stop.set()
        th.join()
        parallel.shutdown()
    r1 = estimate(bm.centroids, bm.sizes, s, p, ("value", "var", "ns"))
    for k in r1:
        np.testing.assert_allclose(r[k], r1[k], equal_nan=True)
    assert seen and all(0 <= f < 1 for f, _t in seen) and "blocks estimated" in seen[-1][1]
    assert cv.stats["n"] > 0 and ss.max_samples >= ss.min_samples
    assert max(stalls) < 1.0                                   # never near Windows' 5 s "Not Responding"


def test_parallel_estimate_can_be_cancelled(gold, ore, monkeypatch):
    from geoviewer.blockmodel import parallel
    from geoviewer.blockmodel.estimate import Cancelled

    monkeypatch.setattr(parallel, "SMALL_ESTIMATE", 0)
    bm = blocks_in_solid(*ore, (10, 10, 5))
    m = VariogramModel(0.5, [Structure("spherical", 5.0, 150.0)])
    p = EstimationParams("ok", model=m, radius=300, max_samples=32)
    asked = []
    t0 = time.time()
    try:
        with pytest.raises(Cancelled):                     # Cancel pressed when the first progress shows
            parallel.estimate(bm.centroids, bm.sizes, gold, p, progress=lambda f, t: asked.append(f),
                              cancel=lambda: bool(asked))
    finally:
        parallel.shutdown()
    assert time.time() - t0 < 30


def test_points_inside_in_workers(ore, monkeypatch):
    from geoviewer.blockmodel import parallel
    from geoviewer.scene.solid import points_inside

    monkeypatch.setattr(parallel, "SMALL_INSIDE", 0)
    pts = np.random.default_rng(0).uniform([1400, 7100, -20], [2000, 7650, 260], (50000, 3))
    try:
        np.testing.assert_array_equal(parallel.points_inside(pts, *ore), points_inside(pts, *ore))
    finally:
        parallel.shutdown()


def test_progress_dialog_is_only_touched_from_the_gui_thread(monkeypatch):
    """The job reports from its own thread; the dialog must be updated in the GUI thread only
    (touching widgets from another thread crashes or hangs the window)."""
    import threading

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QProgressDialog

    from geoviewer.ui.block_manager import run_with_progress

    app = QApplication.instance() or QApplication([])
    main = threading.get_ident()
    threads = set()
    for name in ("setValue", "setLabelText", "setRange"):
        orig = getattr(QProgressDialog, name)
        monkeypatch.setattr(QProgressDialog, name,
                            lambda self, *a, _o=orig: (threads.add(threading.get_ident()), _o(self, *a))[1])

    def work(progress, _cancel):
        assert threading.get_ident() != main                    # the job runs in its own thread
        for i in range(20):
            progress(i / 20, f"step {i}")
            time.sleep(0.02)
        progress(None, "busy")
        time.sleep(0.15)
        return 42

    assert run_with_progress(None, "test", work) == 42
    assert threads == {main}
    app.processEvents()
