"""Drillhole Manager: add / remove holes and edit their collar, survey, geology and assay
rows in the project database, the rock colours (drillhole_styles) and the assay fields.

A hole is edited in place and written with Save (all its tables in one transaction);
rock colours and assay fields are written at once. After every write ``changed`` is
emitted so the drillhole layer is read again.
"""

from __future__ import annotations

import pandas as pd
from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtGui import QColor, QDoubleValidator, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QColorDialog,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..i18n import N_, tr, trc
from ..io.drillhole_store import (ASSAY, COLLAR, GEOLOGY, HOLE_STATES, SURVEY, DrillholeStore, HoleData, check_hole,
                                  state_code)
from . import theme
from .icons import icon

PALETTE = ["#e6194b", "#3cb44b", "#ffe119", "#4363d8", "#f58231", "#911eb4", "#46f0f0", "#f032e6",
           "#bcf60c", "#fabebe", "#008080", "#e6beff", "#9a6324", "#fffac8", "#800000", "#aaffc3"]
COLLAR_LABELS = {"organization": N_("Organization"), "drilling_manager": N_("Drilling manager"),
                 "geology_surveyor": N_("Geology surveyor"), "start_date": N_("Start date"),
                 "end_date": N_("End date"), "state": N_("State")}          # tr() where shown
HEADERS = {"depth": N_("Depth (m)"), "azimuth": N_("Azimuth (°)"), "dip": N_("Dip (°, 0 = down)"),
           "from": N_("From (m)"), "to": N_("To (m)"), "rock": N_("Rock"), "note": N_("Note"),
           "x": N_("X (East)"), "y": N_("Y (North)"), "z": N_("Z (Elevation)"), "max_depth": N_("Length (m)")}


def _header(role: str, default: str) -> str:
    """The column title of a role (translated), else ``default``."""
    return tr(HEADERS[role]) if role in HEADERS else default


def _swatch(color: str, size: int = 14) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(QColor(color))
    return QIcon(pm)


def rock_label(code: str, desc: str) -> str:
    """How a rock is shown: "ST (Schist)", or just the code without a description."""
    return f"{code} ({desc})" if desc else code


def rock_code(label: str) -> str:
    """The code from a shown rock ("ST (Schist)" -> "ST")."""
    return str(label or "").split(" (", 1)[0].strip()


def _text(v) -> str:
    if v is None or (isinstance(v, float) and v != v):
        return ""
    if isinstance(v, float):
        return f"{v:.6g}" if abs(v) < 1e6 else f"{v:.3f}"
    return str(v)


class _DateEdit(QDateEdit):
    """A date that may be empty (shown blank): the empty date is the minimum date."""

    EMPTY = QDate(1900, 1, 1)

    def __init__(self, iso: str = ""):
        super().__init__()
        self.setCalendarPopup(True)
        self.setDisplayFormat("yyyy-MM-dd")
        self.setMinimumDate(self.EMPTY)
        self.setSpecialValueText(" ")                 # the minimum date shows as blank
        d = QDate.fromString(iso or "", "yyyy-MM-dd")
        self.setDate(d if d.isValid() else self.EMPTY)
        self.setToolTip(tr("Pick a date from the calendar; clear it with Delete"))

    def keyPressEvent(self, e) -> None:
        if e.key() in (Qt.Key_Delete, Qt.Key_Backspace) and self.selectedText() == self.text():
            self.setDate(self.EMPTY)
            return
        super().keyPressEvent(e)

    def value(self) -> str:
        return "" if self.date() == self.EMPTY else self.date().toString("yyyy-MM-dd")


def _widget_value(w) -> str:
    if isinstance(w, _DateEdit):
        return w.value()
    if isinstance(w, QComboBox):
        code = w.currentData()
        return "" if code is None else str(code)
    return w.text()


class _Delegate(QStyledItemDelegate):
    """Editors per column: numbers get a number field, the rock column a list of codes."""

    def __init__(self, grid):
        super().__init__(grid.table)
        self.grid = grid

    def createEditor(self, parent, option, index):
        col = self.grid.columns[index.column()]
        if col == self.grid.rock_column:
            box = QComboBox(parent)                   # registered rocks only (Rock colours tab)
            for code, color, desc in self.grid.rock_codes:
                box.addItem(_swatch(color), rock_label(code, desc), code)
            current = rock_code(index.data() or "")
            if current and box.findData(current) < 0:  # a code that is not registered: kept, marked
                box.insertItem(0, tr("{code} (not registered)", code=current), current)
            return box
        ed = QLineEdit(parent)
        if self.grid.info.numeric.get(col):
            v = QDoubleValidator(ed)
            v.setNotation(QDoubleValidator.StandardNotation)
            ed.setValidator(v)
        return ed

    def setEditorData(self, editor, index):
        text = index.data() or ""
        if isinstance(editor, QComboBox):
            i = editor.findData(rock_code(text))
            editor.setCurrentIndex(max(i, 0))
            editor.showPopup()                        # one click opens the list
        else:
            editor.setText(text)

    def setModelData(self, editor, model, index):
        if isinstance(editor, QComboBox):
            model.setData(index, self.grid.label_of(editor.currentData() or ""))
        else:
            model.setData(index, editor.text())


class _Grid(QWidget):
    """One per-hole table (survey, geology or assay) as an editable grid."""

    edited = Signal()

    def __init__(self, hint: str, extra_buttons=()):
        super().__init__()
        self.info = None
        self.columns: list[str] = []
        self.rock_column = None
        self.rock_codes: list[tuple[str, str, str]] = []      # (code, colour, description)
        self._filling = False
        self.table = QTableWidget()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setItemDelegate(_Delegate(self))
        self.table.itemChanged.connect(self._changed)
        add = QPushButton(icon("table-row-plus-after"), tr("Add row"))
        add.setToolTip(tr("Add a row at the end (From = the last To)"))
        add.clicked.connect(self.add_row)
        ins = QPushButton(icon("table-row-plus-before"), tr("Insert row"))
        ins.setToolTip(tr("Insert a row above the selected one"))
        ins.clicked.connect(self.insert_row)
        rem = QPushButton(icon("table-row-remove"), tr("Delete rows"))
        rem.setToolTip(tr("Delete the selected rows"))
        rem.clicked.connect(self.delete_rows)
        row = QHBoxLayout()
        for b in (add, ins, rem):
            row.addWidget(b)
        row.addStretch(1)
        for b in extra_buttons:
            row.addWidget(b)
        v = QVBoxLayout(self)
        v.setContentsMargins(6, 6, 6, 6)
        h = QLabel(hint)
        h.setWordWrap(True)
        h.setStyleSheet(f"color: {theme.MUTED};")
        v.addWidget(h)
        v.addWidget(self.table, 1)
        v.addLayout(row)

    def set_data(self, info, df: pd.DataFrame, rock_codes=()) -> None:
        self._filling = True
        self.info = info
        self.columns = info.fields
        self.rock_column = info.roles.get("rock")
        self.rock_codes = list(rock_codes)
        role_of = {c: k for k, c in info.roles.items()}
        t = self.table
        t.clear()
        t.setColumnCount(len(self.columns))
        t.setHorizontalHeaderLabels([_header(role_of.get(c, ""), c) for c in self.columns])
        head = t.horizontalHeader()
        note = info.roles.get("note")
        for i, c in enumerate(self.columns):         # a note gets the room; the other columns a fixed width
            stretch = c == note or note is None
            head.setSectionResizeMode(i, QHeaderView.Stretch if stretch else QHeaderView.Interactive)
            if note is not None and c != note:
                t.setColumnWidth(i, 190 if c == self.rock_column else 110)
        t.setRowCount(len(df))
        colors = {c: col for c, col, _d in self.rock_codes}
        for r, rec in enumerate(df.to_dict("records")):
            for c, col in enumerate(self.columns):
                it = QTableWidgetItem(_text(rec.get(col)))
                if info.numeric.get(col):
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if col == self.rock_column and it.text():
                    code = it.text()
                    it.setText(self.label_of(code))
                    if code in colors:
                        it.setIcon(_swatch(colors[code]))
                t.setItem(r, c, it)
        self._filling = False

    def label_of(self, code: str) -> str:
        desc = {c: d for c, _col, d in self.rock_codes}
        return rock_label(code, desc.get(code, "")) if code else ""

    def frame(self) -> pd.DataFrame:
        """The rows as edited (rock cells give back the code only)."""
        t = self.table
        rock = self.columns.index(self.rock_column) if self.rock_column in self.columns else -1
        rows = []
        for r in range(t.rowCount()):
            vals = [(t.item(r, c).text().strip() if t.item(r, c) else "") for c in range(len(self.columns))]
            if rock >= 0:
                vals[rock] = rock_code(vals[rock])
            if any(vals):
                rows.append([v or None for v in vals])
        return pd.DataFrame(rows, columns=self.columns)

    def _changed(self, item) -> None:
        if self._filling:
            return
        if self.columns[item.column()] == self.rock_column:
            colors = {c: col for c, col, _d in self.rock_codes}
            code = rock_code(item.text())
            self._filling = True
            item.setText(self.label_of(code))
            item.setIcon(_swatch(colors[code]) if code in colors else QIcon())
            self._filling = False
        self.edited.emit()

    def _new_row(self, at: int) -> None:
        self._filling = True
        self.table.insertRow(at)
        for c in range(len(self.columns)):
            self.table.setItem(at, c, QTableWidgetItem(""))
        r = self.info.roles
        if at > 0 and "from" in r and "to" in r:          # continue from the row above
            prev = self.table.item(at - 1, self.columns.index(r["to"]))
            if prev is not None and prev.text():
                self.table.item(at, self.columns.index(r["from"])).setText(prev.text())
        self._filling = False
        self.table.setCurrentCell(at, 0)
        self.edited.emit()

    def add_row(self) -> None:
        if self.info is not None:
            self._new_row(self.table.rowCount())

    def insert_row(self) -> None:
        if self.info is not None:
            self._new_row(max(self.table.currentRow(), 0))

    def delete_rows(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)
        if rows:
            self.edited.emit()


class _NewHoleDialog(QDialog):
    def __init__(self, parent, collar_info):
        super().__init__(parent)
        self.setWindowTitle(tr("Add drillhole"))
        self.info = collar_info
        f = QFormLayout(self)
        self.hid = QLineEdit()
        self.hid.setPlaceholderText(tr("e.g. WD101"))
        f.addRow(tr("Hole ID"), self.hid)
        self.edits = {}
        for key in ("x", "y", "z", "max_depth"):
            col = collar_info.roles.get(key)
            if col:
                e = QLineEdit()
                e.setValidator(QDoubleValidator(e))
                f.addRow(tr(HEADERS[key]), e)
                self.edits[col] = e
        note = QLabel(tr("The new hole has no survey yet: it is drawn straight down until survey rows "
                         "are added."))
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.MUTED};")
        f.addRow(note)
        b = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        b.accepted.connect(self.accept)
        b.rejected.connect(self.reject)
        f.addRow(b)

    def values(self) -> tuple[str, dict]:
        return self.hid.text().strip(), {c: e.text() for c, e in self.edits.items()}


class DrillholeManager(QDialog):
    changed = Signal()            # the database was written: read the drillhole layer again

    def __init__(self, store: DrillholeStore, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Drillhole Manager"))
        self.resize(1100, 680)
        self.store = store
        self.hid: str | None = None
        self.dirty = False
        self.collar_info = None

        self.tabs = QTabWidget()
        self.tabs.addTab(self._holes_page(), icon("format-list-bulleted"), tr("Drillholes"))
        self.tabs.addTab(self._colours_page(), icon("palette-outline"), tr("Rock colours"))
        self.status = QLabel("")
        self.status.setStyleSheet(f"color: {theme.MUTED};")
        export = QPushButton(icon("microsoft-excel"), tr("Export to Excel ..."))
        export.setToolTip(tr("Write all holes to an Excel file: sheets collar, geology, survey and assay"))
        export.clicked.connect(self.export_excel)
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.close)
        bottom = QHBoxLayout()
        bottom.addWidget(self.status, 1)
        bottom.addWidget(export)
        bottom.addWidget(close)
        v = QVBoxLayout(self)
        v.addWidget(self.tabs, 1)
        v.addLayout(bottom)

    # --- pages ---------------------------------------------------------------------------------
    def _holes_page(self) -> QWidget:
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Find hole ..."))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        self.holes = QListWidget()
        self.holes.currentTextChanged.connect(self._hole_clicked)
        self.count = QLabel("")
        self.count.setStyleSheet(f"color: {theme.MUTED};")
        b_add = QPushButton(icon("plus"), tr("Add hole"))
        b_add.clicked.connect(self.add_hole)
        b_rem = QPushButton(icon("delete-outline"), tr("Remove hole"))
        b_rem.clicked.connect(self.remove_hole)
        row = QHBoxLayout()
        row.addWidget(b_add)
        row.addWidget(b_rem)
        lv.addWidget(self.search)
        lv.addWidget(self.holes, 1)
        lv.addWidget(self.count)
        lv.addLayout(row)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.title = QLabel(tr("Select a hole"))
        self.title.setStyleSheet("font-size: 13pt; font-weight: 600;")
        rv.addWidget(self.title)
        self.hole_tabs = QTabWidget()
        self.collar_page = QWidget()
        self.collar_form = QFormLayout(self.collar_page)
        self.hole_tabs.addTab(self.collar_page, tr("Collar"))
        self.g_survey = _Grid(tr("One row per survey station. Dip in degrees from straight down: "
                                 "0 = straight down, 90 = horizontal. Azimuth clockwise from north."))
        self.g_geology = _Grid(tr("One row per interval: From and To down the hole, the rock (pick from the list - "
                                  "rocks are registered in the Rock colours tab) and a note on the core "
                                  "(may be empty)."))
        b_fadd = QPushButton(icon("table-column-plus-after"), tr("Add field"))
        b_fadd.setToolTip(tr("Add an assay field (e.g. copper) to the assay table, for all holes"))
        b_fadd.clicked.connect(self.add_field)
        b_fren = QPushButton(icon("rename-box"), tr("Rename field"))
        b_fren.clicked.connect(self.rename_field)
        b_frem = QPushButton(icon("table-column-remove"), tr("Remove field"))
        b_frem.setToolTip(tr("Remove an assay field and its values from all holes"))
        b_frem.clicked.connect(self.remove_field)
        self.g_assay = _Grid(tr("One row per sample interval: From, To and the grades. Empty = not assayed."),
                             (b_fadd, b_fren, b_frem))
        for g, name in ((self.g_survey, trc("drillhole", "Survey")), (self.g_geology, tr("Geology")),
                        (self.g_assay, tr("Assay"))):
            g.edited.connect(self._mark_dirty)
            self.hole_tabs.addTab(g, name)
        rv.addWidget(self.hole_tabs, 1)
        self.b_discard = QPushButton(icon("undo"), tr("Discard changes"))
        self.b_discard.clicked.connect(lambda: self._show_hole(self.hid))
        self.b_save = QPushButton(icon("content-save-outline"), tr("Save hole"))
        self.b_save.setToolTip(tr("Write this hole (collar, survey, geology and assay) to the database"))
        self.b_save.clicked.connect(self.save_hole)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.b_discard)
        row.addWidget(self.b_save)
        rv.addLayout(row)

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([230, 870])
        w = QWidget()
        QVBoxLayout(w).addWidget(split)
        return w

    def _colours_page(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        hint = QLabel(tr("The colour of each rock code (drillhole_geology's rock) in the 3D view, the "
                         "sections and the drillhole log. Double-click a row or use Change colour."))
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.MUTED};")
        v.addWidget(hint)
        self.t_rocks = QTableWidget(0, 4)
        self.t_rocks.setHorizontalHeaderLabels([tr("Colour"), tr("Rock"), tr("Description"), tr("Intervals")])
        self.t_rocks.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.t_rocks.setSelectionMode(QAbstractItemView.SingleSelection)
        self.t_rocks.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.t_rocks.verticalHeader().setVisible(False)
        self.t_rocks.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.t_rocks.cellDoubleClicked.connect(lambda r, _c: self.change_colour())
        v.addWidget(self.t_rocks, 1)
        b_col = QPushButton(icon("palette-outline"), tr("Change colour ..."))
        b_col.clicked.connect(self.change_colour)
        b_add = QPushButton(icon("plus"), tr("Add rock code ..."))
        b_add.clicked.connect(self.add_rock)
        b_desc = QPushButton(icon("text-box-edit-outline"), tr("Change description ..."))
        b_desc.clicked.connect(self.change_description)
        b_rem = QPushButton(icon("delete-outline"), tr("Remove colour"))
        b_rem.setToolTip(tr("Remove the colour of the selected rock code (an automatic colour is used then)"))
        b_rem.clicked.connect(self.remove_colour)
        row = QHBoxLayout()
        for b in (b_col, b_add, b_desc, b_rem):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        return w

    # --- loading -------------------------------------------------------------------------------
    def start(self) -> bool:
        """Prepare the tables and fill the window; False (with a message) when the database
        cannot be used."""
        try:
            notes = self.store.prepare()
            self.collar_info = self.store.info(COLLAR)
            self._fill_holes()
            self._fill_rocks()
        except Exception as e:  # noqa: BLE001 - unreachable server, read-only file, ...
            QMessageBox.warning(self.parent(), tr("Drillhole Manager"),
                                tr("The project database cannot be used:\n{database}\n\n{error}",
                                   database=self.store.target.label, error=e))
            return False
        where = self.store.target.short
        self.status.setText(tr("Database: {where}", where=where) + (f"  ({'; '.join(notes)})" if notes else ""))
        if notes:
            self.changed.emit()
        return True

    def _fill_holes(self, select: str | None = None) -> None:
        ids = self.store.hole_ids()
        self.holes.blockSignals(True)
        self.holes.clear()
        self.holes.addItems(ids)
        self.holes.blockSignals(False)
        self.count.setText(tr("{n:,} holes", n=len(ids)))
        self._filter(self.search.text())
        target = select if select in ids else (ids[0] if ids and self.hid not in ids else self.hid)
        if target:
            items = self.holes.findItems(target, Qt.MatchExactly)
            if items:
                self.holes.setCurrentItem(items[0])
        if not ids:
            self._show_hole(None)

    def _filter(self, text: str) -> None:
        t = text.strip().lower()
        for i in range(self.holes.count()):
            it = self.holes.item(i)
            it.setHidden(bool(t) and t not in it.text().lower())

    def _rock_codes(self) -> list[tuple[str, str, str]]:
        styles = self.store.styles()
        return [(c, styles.get(c, "#9aa3ad"), d) for c, d, _n in self.store.rock_codes()]

    def _fill_rocks(self) -> None:
        styles = self.store.styles()
        rows = self.store.rock_codes()
        t = self.t_rocks
        t.setRowCount(len(rows))
        for r, (code, desc, n) in enumerate(rows):
            color = styles.get(code)
            sw = QTableWidgetItem(color.upper() if color else tr("automatic"))
            if color:
                sw.setIcon(_swatch(color, 18))
            t.setItem(r, 0, sw)
            t.setItem(r, 1, QTableWidgetItem(code))
            t.setItem(r, 2, QTableWidgetItem(desc))
            cnt = QTableWidgetItem(f"{n:,}")
            cnt.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            t.setItem(r, 3, cnt)
        t.resizeColumnToContents(0)

    # --- one hole --------------------------------------------------------------------------------
    def _hole_clicked(self, hid: str) -> None:
        if not hid or hid == self.hid:
            return
        if not self._leave_hole():
            items = self.holes.findItems(self.hid or "", Qt.MatchExactly)
            self.holes.blockSignals(True)
            if items:
                self.holes.setCurrentItem(items[0])
            self.holes.blockSignals(False)
            return
        self._show_hole(hid)

    def _leave_hole(self) -> bool:
        """True when the current hole may be left (saved, discarded or not changed)."""
        if not self.dirty:
            return True
        ans = QMessageBox.question(self, tr("Drillhole Manager"), tr("Save the changes to hole {hid}?", hid=self.hid),
                                   QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if ans == QMessageBox.Save:
            return self.save_hole()
        return ans == QMessageBox.Discard

    def _show_hole(self, hid: str | None) -> None:
        self.hid = hid
        while self.collar_form.rowCount():
            self.collar_form.removeRow(0)
        self.collar_edits = {}
        if hid is None:
            self.title.setText(tr("No holes yet - click Add hole"))
            for g in (self.g_survey, self.g_geology, self.g_assay):
                g.table.setRowCount(0)
            self._set_dirty(False)
            return
        data = self.store.load_hole(hid)
        self.hole_id_edit = QLineEdit(hid)
        self.hole_id_edit.setToolTip(tr("Change to rename the hole in every table"))
        self.hole_id_edit.textEdited.connect(self._mark_dirty)
        self.collar_form.addRow(tr("Hole ID"), self.hole_id_edit)
        role_of = {c: k for k, c in self.collar_info.roles.items()}
        for col in self.collar_info.fields:
            value = _text(data.collar.get(col))
            kind = col.lower()
            if kind in ("start_date", "end_date"):
                e = _DateEdit(value)
                e.dateChanged.connect(self._mark_dirty)
            elif kind == "state":
                e = QComboBox()                         # shown as text, kept as its code
                e.addItem("", None)
                for code, text in HOLE_STATES.items():
                    e.addItem(tr(text), code)
                code = state_code(data.collar.get(col))
                e.setCurrentIndex(max(e.findData(code), 0) if code is not None else 0)
                e.currentIndexChanged.connect(self._mark_dirty)
            else:
                e = QLineEdit(value)
                if self.collar_info.numeric.get(col):
                    e.setValidator(QDoubleValidator(e))
                e.textEdited.connect(self._mark_dirty)
            label = _header(role_of.get(col, ""), tr(COLLAR_LABELS[kind]) if kind in COLLAR_LABELS else col)
            self.collar_form.addRow(label, e)
            self.collar_edits[col] = e
        codes = self._rock_codes()
        for g, t in ((self.g_survey, SURVEY), (self.g_geology, GEOLOGY), (self.g_assay, ASSAY)):
            g.set_data(self.store.info(t), data.tables[t], codes)
        self.hole_tabs.setTabText(1, tr("Survey ({n})", n=len(data.tables[SURVEY])))
        self.hole_tabs.setTabText(2, tr("Geology ({n})", n=len(data.tables[GEOLOGY])))
        self.hole_tabs.setTabText(3, tr("Assay ({n})", n=len(data.tables[ASSAY])))
        self._set_dirty(False)

    def _mark_dirty(self, *_a) -> None:
        self._set_dirty(True)

    def _set_dirty(self, on: bool) -> None:
        self.dirty = on and self.hid is not None
        self.b_save.setEnabled(self.dirty)
        self.b_discard.setEnabled(self.dirty)
        if self.hid is not None:
            self.title.setText(tr("Hole {hid}   • not saved", hid=self.hid) if self.dirty
                               else tr("Hole {hid}", hid=self.hid))

    def save_hole(self) -> bool:
        if self.hid is None:
            return True
        collar = {c: _widget_value(e) for c, e in self.collar_edits.items()}
        grids = {SURVEY: self.g_survey, GEOLOGY: self.g_geology, ASSAY: self.g_assay}
        tables = {t: (g.info, g.frame()) for t, g in grids.items()}
        errors, warnings = check_hole(self.collar_info, collar, tables)
        if errors:
            more = "\n" + tr("... and {n} more", n=len(errors) - 15) if len(errors) > 15 else ""
            QMessageBox.warning(self, tr("Save hole"), tr("Please correct these first:") + "\n\n"
                                + "\n".join(errors[:15]) + more)
            return False
        if warnings:
            more = "\n" + tr("... and {n} more", n=len(warnings) - 15) if len(warnings) > 15 else ""
            ans = QMessageBox.question(self, tr("Save hole"), tr("Please check:") + "\n\n" + "\n".join(warnings[:15])
                                       + more + "\n\n" + tr("Save anyway?"))
            if ans != QMessageBox.Yes:
                return False
        new_id = self.hole_id_edit.text().strip()
        try:
            saved = self.store.save_hole(self.hid, HoleData(collar, {t: df for t, (_i, df) in tables.items()}), new_id)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, tr("Save hole"), tr("The hole was not saved:\n{error}", error=e))
            return False
        self.dirty = False
        renamed = saved != self.hid
        self.hid = None if renamed else self.hid
        if renamed:
            self._fill_holes(select=saved)
        else:
            self._show_hole(saved)
        self._fill_rocks()
        self.status.setText(tr("Saved hole {hid}", hid=saved))
        self.changed.emit()
        return True

    def add_hole(self) -> None:
        if not self._leave_hole():
            return
        dlg = _NewHoleDialog(self, self.collar_info)
        while dlg.exec():
            hid, values = dlg.values()
            try:
                self.store.add_hole(hid, values)
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, tr("Add drillhole"), str(e))
                continue
            self.dirty = False
            self._fill_holes(select=hid)
            self.status.setText(tr("Added hole {hid}", hid=hid))
            self.changed.emit()
            return

    def remove_hole(self) -> None:
        if self.hid is None:
            return
        ans = QMessageBox.question(self, tr("Remove drillhole"),
                                   tr("Remove hole {hid} with its survey, geology and assay rows?\n\n"
                                      "This cannot be undone.", hid=self.hid))
        if ans != QMessageBox.Yes:
            return
        hid = self.hid
        n = self.store.remove_hole(hid)
        self.dirty = False
        self.hid = None
        self._fill_holes()
        self._fill_rocks()
        self.status.setText(tr("Removed hole {hid} ({n} rows)", hid=hid, n=n))
        self.changed.emit()

    # --- assay fields ------------------------------------------------------------------------
    def _field_change(self, fn, *args, done: str) -> None:
        if not self._leave_hole():
            return
        try:
            fn(*args)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, tr("Assay fields"), str(e))
            return
        self._show_hole(self.hid)
        self.status.setText(done)
        self.changed.emit()

    def add_field(self) -> None:
        name, ok = QInputDialog.getText(self, tr("Add assay field"), tr("Name of the new field (e.g. copper, ag_ppm):"))
        if ok and name.strip():
            self._field_change(self.store.add_field, name.strip(),
                               done=tr("Added assay field {name}", name=name.strip()))

    def _pick_field(self, title: str) -> str | None:
        fields = self.store.assay_fields()
        if not fields:
            QMessageBox.information(self, title, tr("The assay table has no fields yet."))
            return None
        name, ok = QInputDialog.getItem(self, title, tr("Field:"), fields, 0, False)
        return name if ok else None

    def rename_field(self) -> None:
        old = self._pick_field(tr("Rename assay field"))
        if old:
            new, ok = QInputDialog.getText(self, tr("Rename assay field"), tr("New name for {name}:", name=old),
                                           text=old)
            if ok and new.strip() and new.strip() != old:
                self._field_change(self.store.rename_field, old, new.strip(),
                                   done=tr("Renamed {old} to {new}", old=old, new=new.strip()))

    def remove_field(self) -> None:
        name = self._pick_field(tr("Remove assay field"))
        if name and QMessageBox.question(self, tr("Remove assay field"),
                                         tr("Remove the field {name} and its values from ALL holes?\n\n"
                                            "This cannot be undone.", name=name)) == QMessageBox.Yes:
            self._field_change(self.store.remove_field, name, done=tr("Removed assay field {name}", name=name))

    # --- rock colours ------------------------------------------------------------------------
    def _selected_rock(self) -> str | None:
        r = self.t_rocks.currentRow()
        return self.t_rocks.item(r, 1).text() if r >= 0 and self.t_rocks.item(r, 1) else None

    def change_colour(self) -> None:
        code = self._selected_rock()
        if code is None:
            return
        cur = self.store.styles().get(code, "#9aa3ad")
        c = QColorDialog.getColor(QColor(cur), self, tr("Colour of {code}", code=code))
        if c.isValid():
            self.store.set_style(code, c.name())
            self._rocks_changed(tr("Colour of {code} set to {color}", code=code, color=c.name().upper()))

    def add_rock(self) -> None:
        code, ok = QInputDialog.getText(self, tr("Add rock code"),
                                        tr("Rock code (as used in the geology rows, e.g. GR):"))
        code = code.strip()
        if not ok or not code:
            return
        desc, ok = QInputDialog.getText(self, tr("Add rock code"),
                                        tr("Description of {code} (e.g. Granite):", code=code))
        if not ok:
            return
        used = set(self.store.styles().values())
        color = next((p for p in PALETTE if p not in used), PALETTE[0])
        c = QColorDialog.getColor(QColor(color), self, tr("Colour of {code}", code=code))
        if c.isValid():
            self.store.set_style(code, c.name())
            self.store.set_description(code, desc)
            self._rocks_changed(tr("Added rock code {code}", code=code))

    def change_description(self) -> None:
        code = self._selected_rock()
        if code is None:
            return
        r = self.t_rocks.currentRow()
        cur = self.t_rocks.item(r, 2).text() if self.t_rocks.item(r, 2) else ""
        desc, ok = QInputDialog.getText(self, tr("Rock description"), tr("Description of {code}:", code=code),
                                        text=cur)
        if ok:
            self.store.set_description(code, desc)
            self._rocks_changed(tr("Description of {code} set", code=code))

    def export_excel(self) -> None:
        """All holes to an Excel workbook (sheets collar, geology, survey, assay)."""
        from pathlib import Path

        from PySide6.QtWidgets import QFileDialog

        if self.dirty and QMessageBox.question(
                self, tr("Export to Excel"), tr("Hole {hid} has changes that are not saved. Save them first?\n\n"
                                                "(Unsaved changes are not in the export.)", hid=self.hid)
        ) == QMessageBox.Yes and not self.save_hole():
            return
        t = self.store.target
        start = str((t.path.parent if not t.is_mysql else Path.home()) / "drillholes.xlsx")
        path, _ = QFileDialog.getSaveFileName(self, tr("Export drillholes to Excel"), start,
                                              tr("Excel workbook (*.xlsx)"))
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        try:
            counts = self.store.export_excel(path)
        except PermissionError:
            QMessageBox.warning(self, tr("Export to Excel"),
                                tr("{path}\n\nThe file cannot be written - is it open in Excel? Close it and "
                                   "try again.", path=path))
            return
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, tr("Export to Excel"), tr("The export failed:\n{error}", error=e))
            return
        self.status.setText(tr("Exported to {file}: {counts}", file=Path(path).name,
                               counts=", ".join(f"{k} {v:,}" for k, v in counts.items())))

    def remove_colour(self) -> None:
        code = self._selected_rock()
        if code is not None and code in self.store.styles():
            self.store.remove_style(code)
            self._rocks_changed(tr("Removed the colour of {code}", code=code))

    def _rocks_changed(self, text: str) -> None:
        keep = self._selected_rock()
        self._fill_rocks()
        for r in range(self.t_rocks.rowCount()):
            if self.t_rocks.item(r, 1).text() == keep:
                self.t_rocks.selectRow(r)
        self.g_geology.rock_codes = self._rock_codes()
        if self.hid is not None and not self.dirty:
            self._show_hole(self.hid)                  # rock labels / swatches with the new colours
        self.status.setText(text)
        self.changed.emit()

    def closeEvent(self, e) -> None:
        if not self._leave_hole():
            e.ignore()
            return
        super().closeEvent(e)
