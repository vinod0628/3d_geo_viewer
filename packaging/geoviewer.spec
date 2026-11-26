# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for GeoViewer (one-folder, windowed). Built by packaging/build_release.py.
import os
import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, get_package_paths
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
)

ROOT = Path(SPECPATH).parent
VERSION = re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "geoviewer" / "__init__.py").read_text()).group(1)
_v = tuple(int(x) for x in (VERSION.split(".") + ["0", "0", "0"])[:4])

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=_v, prodvers=_v),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("ProductName", "GeoViewer"),
            StringStruct("FileDescription", "GeoViewer - 3D viewer for Surpac geological data"),
            StringStruct("FileVersion", VERSION),
            StringStruct("ProductVersion", VERSION),
            StringStruct("OriginalFilename", "GeoViewer.exe"),
            StringStruct("InternalName", "GeoViewer"),
        ])]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

# Not used by GeoViewer; keeping them out saves space and avoids pulling in unrelated Qt bindings.
EXCLUDES = [
    "tkinter", "_tkinter", "turtle",
    "PyQt5", "PyQt6", "PySide2",
    "IPython", "ipywidgets", "jupyter", "jupyter_client", "notebook",
    "pytest", "_pytest", "pluggy", "iniconfig",
    "trame", "trame_vtk", "trame_vtk3d", "trame_vtklocal", "trame_matplotlib",
    "matplotlib.backends.backend_tkagg", "matplotlib.backends._backend_tk",
    "matplotlib.backends.backend_webagg", "matplotlib.backends.backend_nbagg",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQuick3D",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.Qt3DCore", "PySide6.QtCharts",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtSql", "PySide6.QtTest",
    "PySide6.QtBluetooth", "PySide6.QtPositioning", "PySide6.QtSerialPort", "PySide6.QtWebSockets",
]

# Languages: the catalogs (geoviewer/i18n/locales/*.json) and Qt's own translation of its buttons
# and dialogs (qtbase_<code>.qm) for each of them - adding a language file is enough.
LOCALES = ROOT / "geoviewer" / "i18n" / "locales"
QT_TRANSLATIONS = Path(get_package_paths("PySide6")[1]) / "translations"
LANGUAGE_DATAS = [(str(LOCALES), "geoviewer/i18n/locales")] + [
    (str(q), "PySide6/translations")
    for f in LOCALES.glob("*.json") for q in QT_TRANSLATIONS.glob(f"qtbase_{f.stem}.qm")]

a = Analysis(
    [str(ROOT / "packaging" / "geoviewer_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=collect_data_files("qtawesome") + LANGUAGE_DATAS,   # icon fonts + charmaps; languages
    hiddenimports=[
        "vtkmodules.qt.QVTKRenderWindowInteractor",
        "vtkmodules.vtkRenderingOpenGL2",
        "vtkmodules.vtkInteractionStyle",
        "vtkmodules.vtkRenderingFreeType",
        "vtkmodules.vtkRenderingAnnotation",
        "pyarrow.vendored.version",
        # run in the estimation worker processes (imported there by name)
        "geoviewer.blockmodel.parallel", "geoviewer.blockmodel.estimate", "geoviewer.blockmodel.variogram",
        "geoviewer.blockmodel.samples", "geoviewer.blockmodel.search", "geoviewer.blockmodel.validate",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GeoViewer",
    icon=str(ROOT / "packaging" / "geoviewer.ico"),
    version=version_info,
    console=os.environ.get("GEOVIEWER_CONSOLE") == "1",  # set GEOVIEWER_CONSOLE=1 for a debug build
    debug=False,
    strip=False,
    upx=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="GeoViewer",
)
