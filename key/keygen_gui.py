"""GeoViewer License Keygen - standalone GUI (the VENDOR's tool).

Fill in the licensee, pick the edition / expiry / features, optionally lock to a machine code,
and click Generate; copy or save the key. Signs with the vendor private key
(``vendor_private_key.pem`` next to this file by default).

    python keygen_gui.py          (needs: PySide6, cryptography)

Keep this tool and the private key secret. Never ship either to customers.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from PySide6.QtCore import QDate, Qt  # noqa: E402
from PySide6.QtGui import QFont, QGuiApplication  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

import license_format as lf  # noqa: E402

DEFAULT_KEY = HERE / "vendor_private_key.pem"

DARK = """
* { font-family: "Segoe UI"; font-size: 10pt; color: #e3e6ea; }
QWidget { background: #1b1d21; }
QGroupBox { border: 1px solid #363b43; border-radius: 6px; margin-top: 10px; padding: 8px 8px 8px 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; color: #9aa3ad; }
QLineEdit, QPlainTextEdit, QComboBox, QDateEdit { background: #2d3138; border: 1px solid #363b43;
    border-radius: 4px; padding: 4px; }
QPushButton { background: #2d3138; border: 1px solid #3a4049; border-radius: 4px; padding: 5px 12px; }
QPushButton:hover { background: #343941; }
QPushButton:default { background: #24476f; border-color: #3d9bff; }
QLabel#title { font-size: 15pt; font-weight: 600; }
"""


class KeygenWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GeoViewer License Keygen")
        self.resize(660, 680)
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(10)

        title = QLabel("GeoViewer License Keygen")
        title.setObjectName("title")
        v.addWidget(title)

        pk_row = QHBoxLayout()
        self.pk_path = QLineEdit(str(DEFAULT_KEY))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_key)
        new = QPushButton("New keypair…")
        new.setToolTip("Create a new vendor keypair (do this once; it invalidates existing keys)")
        new.clicked.connect(self._new_keypair)
        pk_row.addWidget(QLabel("Private key:"))
        pk_row.addWidget(self.pk_path, 1)
        pk_row.addWidget(browse)
        pk_row.addWidget(new)
        v.addLayout(pk_row)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Licensee name, e.g. Acme Mining Ltd")
        form.addRow("Licensee", self.name)
        self.edition = QComboBox()
        for code, nm in lf.EDITIONS.items():
            self.edition.addItem(nm, code)
        form.addRow("Edition", self.edition)
        v.addLayout(form)

        fg = QGroupBox("Features")
        fl = QVBoxLayout(fg)
        tiers = QLabel("Basic: view only    ·    Professional: + edit the database    ·    Ultimate: + export")
        tiers.setStyleSheet("color: #9aa3ad; font-size: 9pt;")
        fl.addWidget(tiers)
        frow = QHBoxLayout()
        self.feature_boxes = {}
        labels = {lf.FEAT_DATABASE: "Edit database (add / edit / remove)", lf.FEAT_EXPORT: "Data export"}
        for bit in (lf.FEAT_DATABASE, lf.FEAT_EXPORT):
            cb = QCheckBox(labels[bit])
            self.feature_boxes[bit] = cb
            frow.addWidget(cb)
        frow.addStretch(1)
        fl.addLayout(frow)
        v.addWidget(fg)

        # dates use the signer's basis (UTC ``_today``), so the field and the cap agree exactly
        today = lf._today()

        def _qd(d):
            return QDate(d.year, d.month, d.day)

        # Validity: every license expires. The cap depends on the edition (Basic up to 30 years,
        # Professional / Ultimate up to 5) - set in _edition_changed().
        self._qd = _qd
        eg = QGroupBox("Validity")
        el = QVBoxLayout(eg)
        self.valid_note = QLabel("")
        self.valid_note.setStyleSheet("color: #9aa3ad; font-size: 9pt;")
        el.addWidget(self.valid_note)
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Expires on"))
        self.expiry = QDateEdit(_qd(lf.max_expiry(1, today)))
        self.expiry.setCalendarPopup(True)
        self.expiry.setDisplayFormat("yyyy-MM-dd")
        self.expiry.setMinimumDate(_qd(today).addDays(1))
        row1.addWidget(self.expiry)
        for label, years in (("+1 year", 1), ("+5 years", 5)):     # add to the current date, click again to extend
            b = QPushButton(label)
            b.clicked.connect(lambda _c=False, y=years: self.expiry.setDate(self.expiry.date().addYears(y)))
            row1.addWidget(b)
        row1.addStretch(1)
        el.addLayout(row1)
        v.addWidget(eg)

        mg = QGroupBox("Machine binding")
        ml = QVBoxLayout(mg)
        self.r_any = QRadioButton("Any computer (not locked)")
        self.r_any.setChecked(True)
        ml.addWidget(self.r_any)
        mrow = QHBoxLayout()
        self.r_bound = QRadioButton("Lock to a machine code:")
        self.machine = QLineEdit()
        self.machine.setPlaceholderText("XXXXX-XXXXX-XXXXX-XXXXX  (from the customer)")
        self.machine.setEnabled(False)
        self.r_bound.toggled.connect(self.machine.setEnabled)
        mrow.addWidget(self.r_bound)
        mrow.addWidget(self.machine, 1)
        ml.addLayout(mrow)
        v.addWidget(mg)

        # edition and machine binding both drive the features and the expiry cap
        self.edition.currentIndexChanged.connect(self._edition_changed)
        self.r_any.toggled.connect(lambda _on: self._update_expiry_cap())
        self.edition.setCurrentIndex(1)
        self._edition_changed()

        gen = QPushButton("Generate license key")
        gen.setDefault(True)
        gen.clicked.connect(self._generate)
        v.addWidget(gen)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(QFont("Consolas", 10))
        self.output.setPlaceholderText("The license key appears here.")
        self.output.setFixedHeight(120)
        v.addWidget(self.output)

        out_row = QHBoxLayout()
        self.summary = QLabel("")
        self.summary.setStyleSheet("color: #9aa3ad;")
        out_row.addWidget(self.summary, 1)
        self.copy_btn = QPushButton("Copy")
        self.copy_btn.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.output.toPlainText()))
        self.save_btn = QPushButton("Save…")
        self.save_btn.clicked.connect(self._save)
        for b in (self.copy_btn, self.save_btn):
            b.setEnabled(False)
            out_row.addWidget(b)
        v.addLayout(out_row)

    def _edition_changed(self) -> None:
        edition = self.edition.currentData()
        feats = lf.edition_features(edition)
        for bit, cb in self.feature_boxes.items():
            cb.setChecked(bool(feats & bit))
        self._update_expiry_cap()

    def _update_expiry_cap(self) -> None:
        """The expiry cap depends on the edition AND the machine binding: an 'Any computer'
        key is at most 1 year, whatever the edition."""
        edition = self.edition.currentData()
        unbound = self.r_any.isChecked()
        cap = lf.issue_max_years(edition, unbound)
        self.expiry.setMaximumDate(self._qd(lf.max_expiry(cap)))   # clamps the current value if needed
        if unbound:
            self.valid_note.setText(f"Any-computer (not locked) keys may expire up to {cap} year from today. "
                                    "Lock to a machine code for a longer license.")
        else:
            self.valid_note.setText(f"{lf.EDITIONS[edition]} licenses may expire up to {cap} years from today"
                                    + ("  (click +5 years again to extend)." if cap > 5 else "."))

    def _browse_key(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Private key", str(HERE), "PEM key (*.pem);;All files (*)")
        if path:
            self.pk_path.setText(path)

    def _new_keypair(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "New private key", str(DEFAULT_KEY), "PEM key (*.pem)")
        if not path:
            return
        info = lf.generate_keypair(path)
        self.pk_path.setText(path)
        QMessageBox.information(self, "New keypair",
                               "Private key written. KEEP IT SECRET and back it up.\n\n"
                               "Paste these into BOTH geoviewer/licensing.py (in the app) and "
                               "license_format.py (here), then rebuild the app:\n\n"
                               f'    _PK_A = "{info["PK_A"]}"\n'
                               f'    _PK_B = "{info["PK_B"]}"\n'
                               f'    _PUBKEY_SHA256 = "{info["SHA256"]}"')

    def _machine_field(self) -> bytes:
        if self.r_any.isChecked():
            return b""
        code = self.machine.text().strip()
        if not code:
            raise ValueError("Enter the customer's machine code, or choose 'Any computer'.")
        return lf.machine_id_from_code(code)

    def _generate(self) -> None:
        try:
            p = Path(self.pk_path.text().strip())
            if not p.is_file():
                raise FileNotFoundError(f"private key not found:\n{p}\n\nUse 'New keypair…' to create one.")
            priv = lf.load_private_key(p)
            name = self.name.text().strip()
            if not name:
                raise ValueError("Enter the licensee name.")
            edition = self.edition.currentData()
            machine = self._machine_field()
            expires = self.expiry.date().toPython()
            years = lf.issue_max_years(edition, not machine)
            max_date = lf.max_expiry(years)
            if expires > max_date:
                what = "An any-computer key" if not machine else self.edition.currentText()
                raise ValueError(f"{what} may expire at most {years} "
                                 f"year{'s' if years != 1 else ''} from today ({max_date}).")
            features = sum(bit for bit, cb in self.feature_boxes.items() if cb.isChecked())
            lic = lf.License(name=name, issued=lf._today(), expires=expires,
                             edition=edition, features=features, machine=machine)
            key = lf.sign_payload(priv, lic)
            check = lf.decode(key)                         # self-check with the public key
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Keygen", str(e))
            return
        self.output.setPlainText(key)
        exp = "perpetual" if check.expires is None else f"expires {check.expires:%Y-%m-%d}"
        where = "any PC" if not check.machine else self.machine.text().strip().upper()
        self.summary.setText(f"{check.name} · {check.edition_name} · "
                             f"{', '.join(check.feature_names) or 'no features'} · {exp} · {where}")
        self.copy_btn.setEnabled(True)
        self.save_btn.setEnabled(True)

    def _save(self) -> None:
        if not self.output.toPlainText():
            return
        safe = "".join(c for c in self.name.text().strip() if c.isalnum() or c in " -_").strip() or "license"
        path, _ = QFileDialog.getSaveFileName(self, "Save license key", str(HERE / f"{safe}.key"),
                                              "License key (*.key)")
        if path:
            Path(path).write_text(self.output.toPlainText().strip() + "\n", encoding="utf-8")


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyleSheet(DARK)
    w = KeygenWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
