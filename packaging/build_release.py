"""Build the GeoViewer Windows release with PyInstaller (offline).

Outputs under distribute\\:
    dist\\GeoViewer\\                        application folder (GeoViewer.exe + libraries)
    release\\GeoViewer_<version>_win64.zip   zip of that folder, ready to hand out
    build\\                                 PyInstaller work folder (deleted unless --keep-work)

Options:
    --skip-build   reuse an existing dist\\GeoViewer (re-test / re-zip only)
    --no-zip       do not create the release zip
    --no-test      skip the smoke test of the built GeoViewer.exe
    --keep-work    keep distribute\\build
    --with-demo    copy surpac_demo\\ into the release as sample data

Run through build_release.bat, or: .venv\\Scripts\\python.exe packaging\\build_release.py
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
import zipfile
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "distribute"
DIST = OUT / "dist"
WORK = OUT / "build"
REL = OUT / "release"
APP = DIST / "GeoViewer"
EXE = APP / "GeoViewer.exe"
SPEC = ROOT / "packaging" / "geoviewer.spec"
LOCK = ROOT / "requirements-offline.txt"
DEMO = ROOT / "surpac_demo"
SMOKE_FILES = ["topo1.dtm", "ore1.dtm", "pit1.str", "topo2.dxf", "block_model.csv", "surpac.accdb"]
# Build/test-only packages: not shipped, so not listed in the third-party notices.
NOT_SHIPPED = {"pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "pefile", "pywin32-ctypes",
               "setuptools", "pytest", "iniconfig", "pluggy", "pip"}

README = """GeoViewer {version}
===================

Read-only 3D viewer for Surpac geological data:
  strings (.str), DTM surfaces / solids (.dtm), block models (CSV export),
  drillholes (the project database - SQLite or MySQL -, Surpac .accdb / .mdb, or
  collar + survey + interval CSV files) and DXF.

Start:  GeoViewer.exe                          opens the last workspace again
        GeoViewer.exe -w "D:\\geo\\project_1"    opens that workspace folder
        GeoViewer.exe file1.dtm file2.str      opens files (files / folders can also be dropped)

Workspace: a folder of data files. The workspace (layers, their order, display settings
        and the view) is saved to geoviewer_workspace.json in the folder when the app closes,
        when another workspace is opened, or with Ctrl+S; paths inside the folder are stored
        relative, so the folder can be moved or copied. A folder without that file opens with all supported files
        found in it. Files opened while no workspace is open start a workspace in their
        folder (or in %LOCALAPPDATA%\\GeoViewer\\workspace if that folder is read-only).
Database: one project database holds the drillholes and the production records, by the
        setting in Settings > Database:
        - MySQL (the default): one database on a MySQL server (server, port, database,
          user, password; the password is stored encrypted for the Windows user). Make an
          empty database once (e.g. CREATE DATABASE geoviewer;) - GeoViewer never makes one
          itself, so a mistyped name shows an error instead of creating a new database.
        - SQLite (nothing to install): the file geodatabase.sqlite in the workspace
          folder. geodatabase.sqlite.bak is a copy made before each session's first change
          to the production records.
        Connection: GeoViewer connects at start (and with Connect in Settings > Database).
          When connected it adds what is missing - tables and fields - and never removes or
          changes tables, fields or rows already there. If the database cannot be reached
          (server down, wrong password, unknown database) a warning says why; the drillhole
          and survey layers are greyed out and Reserve / Mined outline / Drillholes / Survey are
          switched off
          until F5 or Connect finds the database again. The status bar shows the database.
          Changing the database in the settings shows its drillholes and production at once.
        Changes: the table table_updates counts every change of each drillhole_ / prod_
          table (kept by triggers, whoever makes the change). Refresh / automatic refresh
          reads it and shows changed drillholes or production records again.
        Drillhole tables (you fill them, e.g. imported from Excel; GeoViewer only reads):
          drillhole_collar   hole_id, x, y, z, max_depth (optional), organization, drilling_manager,
                             geology_surveyor, start_date, end_date (YYYY-MM-DD), state (a number:
                             1 In work, 2 Completed with ore hit, 3 Completed without ore hit,
                             4 Failed; shown as the text) - all may be empty
          drillhole_survey   hole_id, depth, azimuth, dip (0 = straight down, 90 = horizontal)
          drillhole_geology  hole_id, depth_from, depth_to, rock, note (core description)
          drillhole_assay    hole_id, depth_from, depth_to, then one column per assay (gold, silver ...)
          drillhole_styles   rock, color ("#RRGGBB"): the colour of each rock code
          drillhole_<name>   other interval tables: hole_id, depth_from, depth_to, then any columns
          drillhole_translation (optional) table_name, field_name, code, description
          Common other column names are recognised too (bhid, east, north, rl, from, to ...).
          The layer "Drillholes (database)" is always listed when a workspace opens, and
          F5 / automatic refresh reload it when the tables change.
        Production tables (written by GeoViewer): prod_mined_shapes - one row per mined shape,
        its outline as JSON text [[x, y], ...] in the column "outline" - and prod_settings.
        Records of earlier versions (production.json, production.sqlite) and, when switching
        to MySQL, those in geodatabase.sqlite are moved in automatically.
        Surpac .accdb / .mdb files are still opened directly (read-only, never copied).
        The workspace file (geoviewer_workspace.json) stays a plain text file.
Drillholes: Ctrl+D (database button) opens the Drillhole Manager for the project database:
        the list of holes (Find hole, Add hole, Remove hole) and, for the selected hole, the
        tabs Collar, Survey, Geology and Assay to edit its rows (Add row / Insert row / Delete
        rows; the Hole ID can be changed to rename the hole). Save hole checks the rows first
        (From < To, dip 0 - 180, azimuth 0 - 360, numbers where numbers belong; overlaps and
        depths past the end of the hole are shown as warnings) and writes the hole in one step.
        Assay: Add field / Rename field / Remove field change the assay columns for all holes.
        Rock colours: the colour of each rock code, used in the 3D view, sections and logs.
        Missing tables are made, and a drillhole_styles in Surpac's format is turned into
        (rock, color), when the manager first opens. The drillhole layer is read again after
        every change.
Survey: Ctrl+U (path button) opens the Survey window for the underground traverse survey.
        Pits: New main pit (name, entrance station and its exact X, Y, Z), New branch (starts
        at a station of the selected pit; branches can have branches). Codes are given
        automatically (P1, P2 main pits; P1A, P1B branches of P1; P1A1 ...) and stations are
        numbered P1-01, P1-02 ... - both can be changed.
        Each pit: its rectangular profile (width x height, stations in the backs, floor or
        middle) and whether a passing closure is spread over the legs (Bowditch).
        Setups: Add setup puts the instrument on the last foresight, the backsight on the
        station before and numbers the new foresight. For each setup choose the method:
          Backsight + angles (total station): backsight station, or a known bearing to a
            reference mark (grid / true / magnetic); HA (empty = circle set to the backsight
            bearing), ZA, SD, HT; optional face right readings (averaged, flagged over 30").
          Bearing of the leg (compass): the bearing read for the leg (magnetic by default),
            ZA, SD, HT - no backsight needed.
        Angles are typed 120 30 00 or 120.5 and shown as 120°30'00". HD = SD sin ZA,
        dZ = SD cos ZA + HI - HT. A foresight onto a station that already has coordinates
        (a known station, or back on the entrance) closes the traverse: misclosure and ratio
        are shown and checked against the tolerance.
        Survey settings: closure tolerance (default 1:10,000), magnetic declination
        (true = magnetic + declination, east +) and grid convergence (grid = true - conv.).
        The plan map shows the pit's centre line while typing (wheel zooms, drag pans,
        double-click fits); Save pit writes it. The "Survey (database)" layer draws the
        drives from the survey (centre lines swept with each pit's profile) - no DTM files.
Drillhole size: the drillhole layer's Diameter (Properties, metres; default 4 m) draws the
        holes as cylinders of that diameter (0 = thin lines, the fastest for very many holes).
Layers: "Drillholes (database)", "Survey (database)" and "Mined shapes" cannot be removed (they come from the
        database); untick them to hide them.
Refresh: F5 reloads only the layers whose files changed on disk (display settings are
        kept) and adds data files that appeared in the workspace folder. Automatic
        refresh and its interval are set in Settings.
Identify: Ctrl+I, then click an object (dragging still navigates). Surfaces / solids are
        identified as a whole (area, volume, extent); blocks and points one by one.
        Esc closes the result, Esc again stops the tool.
        A drillhole also opens its log window: collar X/Y/Z, a strip log of the lithology
        (0 m at the top to the end of the hole) and the hole path in XZ section and XY plan
        (centred on the collar, same scale, with scale bars). Print... prints it on one
        A4 page; Save image... writes a 300 dpi PNG.
Measure: Ctrl+M, then click two points: distance, plan distance, dX/dY/dZ, bearing
        (clockwise from north), inclination (+ up / - down) and gradient. Points snap to
        a nearby vertex; in 2D views empty space can be clicked too.
Cross-section: Ctrl+K in a 2D view (plan XY, or XZ / YZ). Click points along the section
        line, then right-click or Enter (Backspace removes the last point, Esc cancels).
        The points editor opens: type coordinates, add / remove / reorder points, choose
        whether the drillhole rock legend goes on the exported page, then Draw section.
        The section window shows surfaces and solids cut exactly, blocks the line passes
        through, and strings / drillholes (coloured by lithology, with a legend) within
        the corridor (+/- width) projected onto it, with a scale bar and grid (scale 1:1).
        Export writes a printable 300 dpi PNG page (A4 / A3, white): the plan with the
        section line on top, the section below; coordinates outside the map frames,
        scale bars at the bottom left of each map. The section is fitted to its frame at a
        standard scale (shown in its title, e.g. 1:2,500; true when printed at 100 %).
Reserve: Ctrl+E (pickaxe button) opens the Reserve window: total reserve, produced and
        remaining ore (tonnes, grade, metal in kg) as of a chosen date, the list of
        mined shapes and a summary by month (both can be exported as CSV).
        The total reserve is the blocks whose centre is inside the reserve solid (e.g. ore1);
        Setup chooses the block model, the solid, the density (attribute or fixed value)
        and the grade (g/t or %). Sensible choices are made automatically.
        To record mining: Draw mined outline (or Ctrl+L in a 2D view), click the corners of
        the mined area, right-click or Enter. Then set the date and the levels From / To:
        the chart shows the ore under the outline level by level (already mined parts in
        red) - drag the From / To lines or use "Use ore bottom / top" or "Continue from
        last cut". The cut shows in the view as a see-through solid, with its tonnes, grade,
        ore share and warnings (e.g. no ore between From and To). Save.
        Or Load mined solid ...: a closed Surpac .dtm (e.g. a surveyed stope solid) is the
        mined shape instead of an outline - date, name and note, with the same live figures.
        Blocks can be mined bit by bit: each block is split into 8 sample points (2 x 2 x 2), and a
        point is counted once, by the earliest cut containing it. The mined shapes are
        saved in the project database (see Database; the block model file is never
        changed). Mined blocks (at least half mined) are shown by the block model's
        "Mined blocks" property: Show (like the other blocks), Gray (plain gray),
        Transparent or Hidden; the colours by grade stay as they are. The attributes status (Unmined /
        Partly mined / Mined) and mined_fraction (0 - 1) can be used in constraints. The layer
        "Mined shapes" shows the cuts coloured by month, also in cross-sections.

Mouse:  left drag = rotate (or pan in pan mode / 2D views), middle drag = pan,
        right drag or wheel = zoom.

License: GeoViewer needs a license key. The first time it runs it shows the License
        window with this computer's machine code (XXXXX-XXXXX-XXXXX-XXXXX, based on the
        hardware - it does not change when Windows is reinstalled). Send that code to the
        vendor, who returns a license key; paste it in and click Activate. The License button
        on the toolbar reopens the window. Editions: Basic (view data), Professional (also
        add / edit / remove the project database), Ultimate (also export maps and sections).
        A key can be time-limited and locked to one machine code.

Keys:   Ctrl+Shift+O open workspace   Ctrl+Shift+S save workspace as (copy to another folder)
        Ctrl+O open files     F5 refresh       Ctrl+, settings      Ctrl+I identify   Ctrl+M measure
        Ctrl+K cross-section (2D views)   Ctrl+E reserve   Ctrl+L mined outline (2D views)   Ctrl+D drillholes
        Ctrl+U survey
        Ctrl+T show/hide toolbar     F11 full screen (the toolbar becomes a round button), Esc exits
        Ctrl+R rotate mode    Ctrl+P pan mode    Home zoom to all data (3D: also resets the view)
        Ctrl+= / Ctrl+- zoom  Ctrl+1 3D, Ctrl+2 XY plan, Ctrl+3 XZ, Ctrl+4 YZ section
        Ctrl+G map grid       Ctrl+5 perspective / orthographic 3D view (2D views are orthographic)

Drillhole CSV: select the collar, survey and interval files together in the open dialog.
Block model CSV: one row per block with centroid X/Y/Z, block size X/Y/Z and attributes.
Block model constraint (properties, Constraint): conditions on DTMs (inside / outside a
        solid, above / below a surface) and block values (e.g. gold >= 4), combined by
        and / or or an expression such as  a and (b or not c). The block centre is tested.
Layers: right-click > Duplicate view shows the same file again with its own settings
        (e.g. two block model views with different constraints); Rename... names a layer.
Parsed block models are cached in %LOCALAPPDATA%\\GeoViewer\\cache (safe to delete).

Requirements: Windows 10/11 x64, OpenGL 3.2+ graphics driver.
Third-party components and licences: THIRD_PARTY_NOTICES.txt
"""


def version() -> str:
    text = (ROOT / "geoviewer" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1)


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def write_notices(path: Path) -> None:
    """List every shipped package with its version and licence (from package metadata)."""
    wanted = set()
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if "==" in line:
            wanted.add(norm(line.split("==")[0]))
    rows = []
    for d in metadata.distributions():
        name = d.metadata["Name"]
        if norm(name) not in wanted or norm(name) in NOT_SHIPPED:
            continue
        lic = d.metadata.get("License-Expression") or d.metadata.get("License") or ""
        lic = lic.strip().splitlines()[0] if lic.strip() else ""
        if not lic or len(lic) > 60:
            classifiers = [c.split("::")[-1].strip() for c in d.metadata.get_all("Classifier") or []
                           if c.startswith("License ::")]
            lic = ", ".join(classifiers) or lic[:60] or "see package"
        home = d.metadata.get("Home-page") or ""
        if not home:
            for url in d.metadata.get_all("Project-URL") or []:
                home = url.split(",", 1)[-1].strip()
                break
        rows.append((name, d.version, lic, home))
    rows.sort(key=lambda r: r[0].lower())
    lines = [
        "GeoViewer uses the following third-party components.",
        "Qt / PySide6 are used under the LGPL v3 and are dynamically linked (separate DLLs",
        "in this folder), so they can be replaced by the user.",
        "",
        f"{'Package':28} {'Version':14} Licence",
        "-" * 90,
    ]
    lines += [f"{n:28} {v:14} {lic}" + (f"\n{'':43} {h}" if h else "") for n, v, lic, h in rows]
    lines += ["", f"Python {sys.version.split()[0]} - PSF License"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def folder_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def build() -> None:
    print(f"[build] PyInstaller -> {APP}")
    t = time.perf_counter()
    cmd = [sys.executable, "-m", "PyInstaller", str(SPEC), "--noconfirm", "--clean",
           "--log-level", "WARN", "--distpath", str(DIST), "--workpath", str(WORK)]
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        raise SystemExit(f"PyInstaller failed (exit {r.returncode})")
    print(f"[build] done in {time.perf_counter() - t:.0f}s")


def smoke_test() -> None:
    import tempfile

    # a copy of the demo files (the app may write next to them, e.g. the workspace file)
    with tempfile.TemporaryDirectory(prefix="geoviewer_smoke_") as tmp:
        files = []
        for f in SMOKE_FILES:
            if (DEMO / f).exists():
                shutil.copy2(DEMO / f, Path(tmp) / f)
                files.append(str(Path(tmp) / f))
        print(f"[test] {EXE.name} --smoke-test with {len(files)} demo files (a window opens briefly)")
        t = time.perf_counter()
        try:
            r = subprocess.run([str(EXE), "--smoke-test", *files], timeout=180,
                               capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            raise SystemExit("[test] FAILED: GeoViewer.exe did not exit within 180 s")
        if r.returncode != 0:
            sys.stderr.write(r.stderr[-3000:])
            raise SystemExit(f"[test] FAILED: GeoViewer.exe exit code {r.returncode}")
        print(f"[test] OK in {time.perf_counter() - t:.0f}s")


def make_zip(ver: str) -> Path:
    REL.mkdir(parents=True, exist_ok=True)
    out = REL / f"GeoViewer_{ver}_win64.zip"
    print(f"[zip] {out}")
    tmp = out.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in sorted(APP.rglob("*")):
            if f.is_file():
                z.write(f, Path("GeoViewer") / f.relative_to(APP))
    tmp.replace(out)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--no-zip", action="store_true")
    ap.add_argument("--no-test", action="store_true")
    ap.add_argument("--keep-work", action="store_true")
    ap.add_argument("--with-demo", action="store_true")
    args = ap.parse_args()

    ver = version()
    print(f"GeoViewer {ver} release build")
    if not args.skip_build:
        build()
    if not EXE.exists():
        raise SystemExit(f"{EXE} not found - run without --skip-build")

    (APP / "README.txt").write_text(README.format(version=ver), encoding="utf-8")
    write_notices(APP / "THIRD_PARTY_NOTICES.txt")
    if args.with_demo and DEMO.exists():
        dst = APP / "sample_data"
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(DEMO, dst)
        print(f"[demo] copied {DEMO.name} -> {dst}")

    if not args.no_test:
        smoke_test()
    zip_path = None if args.no_zip else make_zip(ver)
    if not args.keep_work and WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)

    print()
    print(f"Application : {APP}  ({folder_size(APP) / 1e6:.0f} MB)")
    if zip_path:
        print(f"Release zip : {zip_path}  ({zip_path.stat().st_size / 1e6:.0f} MB)")
    print("BUILD OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
