"""GUI tab "Texturen": browse the cooked Texture2D assets, preview them and export PNG. Game data
(arcpak.game / arcpak.texture) is loaded in a background thread the first time the tab is opened."""
import os
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                             QListWidgetItem, QMessageBox, QPushButton, QSplitter, QVBoxLayout, QWidget)

from arcpak.config import APP_DIR
from i18n import tr

HERE = APP_DIR                      # writable root (repo, or the folder of MapExporter.exe)
MIPS = (256, 512, 1024, 2048, 4096)
LIST_MAX = 3000                     # 14 000 rows make the list sluggish; filtering shows the rest


class CatalogueWorker(QThread):
    done = pyqtSignal(object, str)              # catalogue, error

    def run(self):
        try:
            from arcpak import texture
            self.done.emit(texture.catalogue(), "")
        except Exception as e:
            self.done.emit(None, f"{type(e).__name__}: {e}")


class ImageWorker(QThread):
    done = pyqtSignal(object, object, object, str)      # item, PNG bytes, info, error

    def __init__(self, parent, item, max_size: int, alpha: bool):
        super().__init__(parent)
        self.item, self.max_size, self.alpha = item, max_size, alpha

    def run(self):
        try:
            from arcpak import texture
            img = texture.load_texture(self.item.gpath, self.max_size, display=True)
            if img is None:
                self.done.emit(self.item, None, None, tr("Kein Bild dekodierbar — Pixelformat unsupported?"))
                return
            self.done.emit(self.item, texture.to_png(img, opaque=not self.alpha),
                           texture.texture_info(self.item.gpath), "")
        except Exception as e:
            self.done.emit(self.item, None, None, f"{type(e).__name__}: {e}")


class TexturesTab(QWidget):
    def __init__(self, parent=None, state=None):
        super().__init__(parent)
        self.catalogue = None
        self.png, self.current, self.info_data = None, None, None
        self.loader = self.worker = None

        self.filter = QLineEdit()
        self.filter.setPlaceholderText(tr("Textur filtern…"))
        self.filter.textChanged.connect(self.fill_list)
        self.items = QListWidget()
        self.items.currentItemChanged.connect(lambda *_: self.load_selected())
        left = QVBoxLayout()
        left.addWidget(self.filter)
        left.addWidget(self.items, 1)
        lw = QWidget(); lw.setLayout(left)

        self.size_box = QComboBox()
        for m in MIPS:
            self.size_box.addItem(tr("bis {m} px", m=m), m)
        self.size_box.setCurrentIndex(MIPS.index(2048))
        self.size_box.currentIndexChanged.connect(lambda *_: self.load_selected())
        self.opt_alpha = QCheckBox(tr("Alphakanal behalten"))
        self.opt_alpha.setToolTip(tr("Roughness und Masken sitzen oft im Alphakanal; ohne ihn ist das PNG dreikanalig"))
        self.opt_alpha.toggled.connect(lambda *_: self.load_selected())
        top = QHBoxLayout()
        top.addWidget(QLabel(tr("Größe:"))); top.addWidget(self.size_box)
        top.addWidget(self.opt_alpha); top.addStretch(1)

        self.preview = QLabel("—")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(420, 380)
        self.preview.setStyleSheet("background: #26282c; border: 1px solid #3a3d42;")
        self.preview.setScaledContents(False)
        self.info = QLabel("—")
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        btns = QHBoxLayout()
        self.btn_export = QPushButton(tr("Als PNG exportieren"))
        self.btn_export.setStyleSheet("font-weight: bold; padding: 6px 18px;")
        self.btn_export.clicked.connect(self.export)
        self.btn_folder = QPushButton(tr("Ordner öffnen"))
        self.btn_folder.clicked.connect(self.open_folder)
        for b in (self.btn_export, self.btn_folder):
            btns.addWidget(b)
        btns.addStretch(1)
        self.btn_export.setEnabled(False)

        right = QVBoxLayout()
        right.addLayout(top)
        right.addWidget(self.preview, 1)
        right.addWidget(self.info)
        right.addLayout(btns)
        rw = QWidget(); rw.setLayout(right)

        split = QSplitter()
        split.addWidget(lw); split.addWidget(rw)
        split.setStretchFactor(1, 1)
        split.setSizes([340, 880])
        lay = QVBoxLayout(self)
        lay.addWidget(split)
        if state:
            self._catalogue_done(state, "")

    def state(self):
        """Catalogue to hand over to a rebuilt window (None if not loaded yet)."""
        return self.catalogue

    def release(self):
        if self.worker and self.worker.isRunning():
            self.worker.done.disconnect()

    # ---------- catalogue ----------
    def ensure_loaded(self):
        """Load the texture catalogue in the background (first time the tab is shown)."""
        if self.catalogue is not None or self.loader is not None:
            return
        self.items.clear()
        self.items.addItem(tr("Lade Spieldaten…"))
        self.loader = CatalogueWorker(self)
        self.loader.done.connect(self._catalogue_done)
        self.loader.start()

    def _catalogue_done(self, cat, err):
        if err:
            self.items.clear()
            self.items.addItem(tr("Fehler: {err}", err=err))
            return
        self.catalogue = cat
        self.fill_list()

    def fill_list(self):
        if not self.catalogue:
            return
        flt = self.filter.text().strip().lower()
        self.items.blockSignals(True)
        self.items.clear()
        shown = 0
        for it in self.catalogue:
            label = f"{it.name}  —  {it.folder}" if it.folder else it.name
            if flt and flt not in label.lower():
                continue
            if shown >= LIST_MAX:
                shown += 1
                continue
            li = QListWidgetItem(f"{max(it.size, it.bulk) / 1e6:6.2f} MB  {label}")
            li.setData(Qt.ItemDataRole.UserRole, it)
            self.items.addItem(li)
            shown += 1
        self.items.blockSignals(False)
        self.info.setText(tr("{n} Texturen", n=self.items.count())
                          + (tr(" — {rest} ausgeblendet, bitte filtern", rest=shown - self.items.count())
                             if shown > self.items.count() else ""))

    # ---------- preview ----------
    def selected_item(self):
        li = self.items.currentItem()
        return li.data(Qt.ItemDataRole.UserRole) if li else None

    def load_selected(self):
        it = self.selected_item()
        if it is None:
            return
        if self.worker and self.worker.isRunning():
            self.worker.done.disconnect()
        self.info.setText(tr("Lade {name}…", name=it.name))
        self.btn_export.setEnabled(False)
        self.worker = ImageWorker(self, it, self.size_box.currentData(), self.opt_alpha.isChecked())
        self.worker.done.connect(self._image_done)
        self.worker.start()

    def _image_done(self, item, png, info, err):
        if item is not self.selected_item():
            return
        if err or png is None:
            self.current, self.png, self.info_data = item, None, None
            self.preview.setText("—")
            self.info.setText(f"<b>{item.name}</b><br><span style='color:#e88'>{err}</span>")
            self.btn_export.setEnabled(False)
            return
        self.current, self.png, self.info_data = item, png, info
        pm = QPixmap()
        pm.loadFromData(png)
        self.preview.setPixmap(pm.scaled(640, 520, Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.SmoothTransformation))
        mips = info.get("mips") or []
        gr = ", ".join(f"{m['w']}×{m['h']}" for m in mips[:4]) + (" …" if len(mips) > 4 else "")
        self.info.setText(tr("<b>{name}</b> — {fmt}, {w}×{h}, {n} MIPS", name=item.name, fmt=info.get("format"),
                             w=pm.width(), h=pm.height(), n=len(mips))
                          + f"<br>{tr('Mips')}: <small>{gr}</small>"
                          + "<br>" + tr("{png} MB PNG, {raw} MB Rohdaten im Spiel",
                                        png=f"{len(png) / 1e6:.1f}", raw=f"{info.get('bytes', 0) / 1e6:.1f}")
                          + " · " + tr("Farbraum: {raum}", raum=info.get("farbraum") or "?"))
        self.btn_export.setEnabled(True)

    # ---------- export ----------
    def export(self):
        it = self.current
        if it is None or not self.png:
            return None
        from arcpak import texture
        path = texture.out_path(it, base=str(HERE / "out" / "textures"))
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "wb") as f:
                f.write(self.png)
        except Exception as e:
            QMessageBox.warning(self, tr("Texturen"), f"{type(e).__name__}: {e}")
            return None
        self.info.setText(self.info.text() + "<br>" + tr("exportiert → {path}", path=os.path.relpath(path, HERE)))
        return path

    def open_folder(self):
        from arcpak.config import open_folder
        open_folder(HERE / "out" / "textures")
