"""GUI tab "Audio": browse the cooked sounds and export them. Game data (arcpak.game /
arcpak.audio) is loaded in a background thread the first time the tab is opened.

Two formats live in the game data: raw 16-bit PCM (exports as playable WAV) and RAD BinkA blocks
(re-embedded into a UEBA container). "▶ Anhören" decodes BinkA through an external FFmpeg; MapExporter
itself has no BinkA decoder and says so in the UI."""
import os
import time
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                             QMessageBox, QProgressBar, QPushButton, QSplitter, QVBoxLayout, QWidget)

from i18n import tr

HERE = Path(__file__).resolve().parent.parent
LIST_MAX = 4000                     # 55 000 rows make the list sluggish; filtering shows the rest


class CatalogueWorker(QThread):
    done = pyqtSignal(object, str)              # catalogue, error

    def run(self):
        try:
            from arcpak import audio
            self.done.emit(audio.catalogue(), "")
        except Exception as e:
            self.done.emit(None, f"{type(e).__name__}: {e}")


class ExportWorker(QThread):
    progress = pyqtSignal(int, int)             # done, total
    done = pyqtSignal(int, int, int, int, int, int, float)      # wav, binka, raw, failed, bytes, skipped, seconds

    def __init__(self, parent, items, out_dir: str, raw: bool):
        super().__init__(parent)
        self.items, self.out_dir, self.raw, self.stop = items, out_dir, raw, False

    def run(self):
        from arcpak import audio
        t0 = time.time()
        n = {"wav": 0, "binka": 0, "raw": 0}
        fails = total = skipped = 0
        for i, it in enumerate(self.items):
            if self.stop:
                break
            if not it.ubulk:
                skipped += 1                    # SoundCue/Attenuation/Curve: nothing to export
                continue
            try:
                if self.raw:
                    path, kind = audio.export_raw(it, audio.out_path(it, ".ubulk", self.out_dir)), "raw"
                elif it.pcm:
                    path, kind = audio.export_wav(it, audio.out_path(it, ".wav", self.out_dir)), "wav"
                else:
                    path, kind = audio.export_binka(it, audio.out_path(it, ".binka", self.out_dir)), "binka"
                total += os.path.getsize(path)
                n[kind] += 1
            except Exception:
                fails += 1
            if i % 100 == 99:
                self.progress.emit(i + 1, len(self.items))
        self.progress.emit(len(self.items), len(self.items))
        self.done.emit(n["wav"], n["binka"], n["raw"], fails, total, skipped, time.time() - t0)

    def cancel(self):
        self.stop = True


class PlayWorker(QThread):
    """Prepare one sound as a temporary WAV: PCM directly, BinkA via external FFmpeg."""
    done = pyqtSignal(str, str)                 # WAV path ("" on error), error ("" = ok)
    way = ""

    def __init__(self, parent, item):
        super().__init__(parent)
        self.item = item

    def run(self):
        import tempfile
        from arcpak import audio
        try:
            data, self.way = audio.playback_wav(self.item.gpath)
            fd, path = tempfile.mkstemp(prefix="mapexport_klang_", suffix=".wav")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        except Exception as e:
            self.done.emit("", f"{type(e).__name__}: {e}")
            return
        self.done.emit(path, "")


class AudioTab(QWidget):
    def __init__(self, parent=None, state=None):
        super().__init__(parent)
        self.catalogue = None
        self.current = None
        self.loader = self.worker = self.player = None

        self.filter = QLineEdit()
        self.filter.setPlaceholderText(tr("Klang filtern…"))
        self.filter.textChanged.connect(self.fill_list)
        self.opt_meta = QCheckBox(tr("Meta-Assets zeigen"))
        self.opt_meta.setToolTip(tr("SoundCues, Attenuationen und Curves haben keine Klangdaten — nur Namen"))
        self.opt_meta.toggled.connect(self.fill_list)
        row = QHBoxLayout()
        row.addWidget(self.filter, 1); row.addWidget(self.opt_meta)
        self.items = QListWidget()
        self.items.currentItemChanged.connect(lambda *_: self.show_item())
        left = QVBoxLayout()
        left.addLayout(row)
        left.addWidget(self.items, 1)
        lw = QWidget(); lw.setLayout(left)

        self.info = QLabel("—")
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.note = QLabel(tr("Roh-PCM-Klänge werden zu WAV; BinkA ist ein proprietärer Codec von RAD Game Tools — "
                             "MapExporter baut dafür die Soundblöcke in einen UEBA-Container (in Unreal abspielbar). "
                             "▶ Anhören dekodiert auch BinkA — über das externe FFmpeg, das UEBA ab Version 8.1 liest."))
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: #9aa0a6;")

        self.opt_raw = QCheckBox(tr("unveränderte .ubulk"))
        self.opt_raw.setToolTip(tr("Die Klangdatei byte-genau kopieren, statt den Container neu aufzubauen"))
        btns = QHBoxLayout()
        self.btn_export = QPushButton(tr("Exportieren"))
        self.btn_export.setStyleSheet("font-weight: bold; padding: 6px 18px;")
        self.btn_export.clicked.connect(lambda: self.export([self.current] if self.current else []))
        self.btn_play = QPushButton(tr("▶ Anhören"))
        self.btn_play.setToolTip(tr("Roh-PCM sofort; BinkA über das externe FFmpeg (UEBA liest FFmpeg ab 8.1)"))
        self.btn_play.clicked.connect(self.play)
        self.btn_all = QPushButton(tr("Alle exportieren"))
        self.btn_all.clicked.connect(self.export_all)
        self.btn_cancel = QPushButton(tr("Abbrechen"))
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(lambda: self.worker and self.worker.cancel())
        self.btn_json = QPushButton(tr("Katalog als JSON"))
        self.btn_json.clicked.connect(self.write_json)
        self.btn_folder = QPushButton(tr("Ordner öffnen"))
        self.btn_folder.clicked.connect(self.open_folder)
        for b in (self.btn_export, self.btn_play, self.btn_all, self.btn_cancel,
                  self.opt_raw, self.btn_json, self.btn_folder):
            btns.addWidget(b)
        btns.addStretch(1)
        self.btn_export.setEnabled(False)
        self.btn_play.setEnabled(False)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        self.step = QLabel("")

        right = QVBoxLayout()
        right.addWidget(self.info)
        right.addWidget(self.note)
        right.addLayout(btns)
        right.addWidget(self.progress)
        right.addWidget(self.step)
        right.addStretch(1)
        rw = QWidget(); rw.setLayout(right)

        split = QSplitter()
        split.addWidget(lw); split.addWidget(rw)
        split.setStretchFactor(1, 1)
        split.setSizes([420, 800])
        lay = QVBoxLayout(self)
        lay.addWidget(split)
        if state:
            self._catalogue_done(state, "")

    def state(self):
        """Catalogue to hand over to a rebuilt window (None if not loaded yet)."""
        return self.catalogue

    def release(self):
        for w in (self.worker, self.player, self.loader):
            if w is not None and w.isRunning():
                try:
                    w.done.disconnect()
                except (RuntimeError, TypeError):
                    pass

    # ---------- catalogue ----------
    def ensure_loaded(self):
        """Load the audio catalogue in the background (first time the tab is shown)."""
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
        with_data = [it for it in self.catalogue if it.ubulk]
        hidden = len(self.catalogue) - len(with_data)
        pool = self.catalogue if self.opt_meta.isChecked() else with_data
        self.items.blockSignals(True)
        self.items.clear()
        shown = 0
        for it in pool:
            if flt and flt not in it.name.lower() and flt not in it.folder.lower():
                continue
            if shown >= LIST_MAX:
                shown += 1
                continue
            fmt = "WAV" if it.pcm else ("UEBA" if it.ubulk else "—")
            dauer = f"{it.duration:5.2f} s" if it.duration else "   —  "
            li = QListWidgetItem(f"{dauer}  {it.rate or 0:>5} Hz  {(it.channels or 0) or '?'}ch  "
                                f"{it.size / 1e6:7.3f} MB  {fmt:>4}  {it.name}")
            li.setData(Qt.ItemDataRole.UserRole, it)
            self.items.addItem(li)
            shown += 1
        self.items.blockSignals(False)
        extra = tr(" — {rest} ausgeblendet, bitte filtern", rest=shown - self.items.count()) \
            if shown > self.items.count() else ""
        if not self.opt_meta.isChecked() and hidden:
            extra += tr(" — {n} Meta-Assets ausgeblendet (Häkchen zeigen)", n=hidden)
        self.step.setText(tr("{n} Klänge", n=self.items.count()) + extra)

    # ---------- selection ----------
    def selected_item(self):
        li = self.items.currentItem()
        return li.data(Qt.ItemDataRole.UserRole) if li else None

    def show_item(self):
        it = self.selected_item()
        self.current = it
        self.btn_export.setEnabled(it is not None and it.ubulk)
        self.btn_play.setEnabled(it is not None and it.ubulk and not (self.player and self.player.isRunning()))
        if it is None:
            self.info.setText("—")
            return
        dauer = f"{it.duration:.3f} s" if it.duration else tr("unbekannt")
        fmt = (tr("<b>Roh-PCM (16-bit)</b> → WAV") if it.pcm
               else (tr("<b>BinkA</b> → UEBA-Container (.binka)") if it.ubulk else tr("nur Meta-Daten")))
        lines = [f"<b>{it.name}</b>",
                 f"<code>{it.gpath}</code>",
                 tr("Ordner: {folder}", folder=it.folder or "—"),
                 f"{dauer} · {it.rate or '?'} Hz · {it.channels or '?'} ch · {it.samples or '?'} Samples",
                 tr("Klangdatei {size} MB", size=f"{it.size / 1e6:.3f}"),
                 fmt]
        self.info.setText("<br>".join(lines))

    # ---------- export ----------
    def export_list(self):
        """All rows the current filter shows (not the whole catalogue)."""
        return [self.items.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.items.count())]

    def export_all(self):
        jobs = self.export_list()
        if not jobs:
            return
        if len(jobs) > 200:
            if QMessageBox.question(self, tr("Audio"),
                                    tr("{n} Klänge exportieren?\nDas kann je nach Format einige Minuten dauern.",
                                       n=len(jobs))) != QMessageBox.StandardButton.Yes:
                return
        self.export(jobs)

    def export(self, jobs):
        jobs = [j for j in jobs if j is not None]
        if not jobs:
            return
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, tr("Audio"), tr("Es läuft schon ein Export — Fortschritt beobachten."))
            return
        out_dir = str(HERE / "out" / "audio")
        self.btn_export.setEnabled(False); self.btn_all.setEnabled(False); self.btn_cancel.setEnabled(True)
        self.progress.setValue(0); self.progress.setVisible(True)
        self.step.setText(tr("Exportiere {n} Klänge …", n=len(jobs)))
        self.worker = ExportWorker(self, jobs, out_dir, self.opt_raw.isChecked())
        self.worker.progress.connect(lambda d, t: self.progress.setValue(int(1000 * d / max(t, 1))))
        self.worker.done.connect(self._export_done)
        self.worker.start()

    def _export_done(self, wav, binka, raw, fails, total, skipped, secs):
        self.btn_export.setEnabled(self.current is not None and self.current.ubulk)
        self.btn_all.setEnabled(True); self.btn_cancel.setEnabled(False)
        self.progress.setVisible(False)
        parts = [f"{n} {k}" for n, k in ((wav, "WAV"), (binka, "UEBA"), (raw, ".ubulk")) if n]
        self.step.setText(tr("{parts} → {dir} ({size} MB, {sec:.0f} s)", parts=", ".join(parts) or "—",
                             dir=os.path.relpath(HERE / "out" / "audio", HERE), size=f"{total / 1e6:.1f}", sec=secs)
                          + (tr(", {n} Fehler", n=fails) if fails else "")
                          + (tr(", {n} ohne Klangdaten übersprungen", n=skipped) if skipped else ""))

    # ---------- playback ----------
    def play(self):
        if self.current is None or not self.current.ubulk or (self.player and self.player.isRunning()):
            return
        self.btn_play.setEnabled(False)
        self.step.setText(tr("Klang wird vorbereitet …"))
        self.player = PlayWorker(self, self.current)
        self.player.done.connect(self._play_done)
        self.player.start()

    def _play_done(self, path, err):
        way = self.player.way if self.player else ""
        self.btn_play.setEnabled(self.current is not None and self.current.ubulk)
        if err:
            self.step.setText(tr("Abspielen nicht möglich: {err}", err=err))
            return
        opened = QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        hint = tr("BinkA über FFmpeg dekodiert") if way == "ffmpeg" else tr("Roh-PCM")
        self.step.setText(tr("WAV → {path} ({hint}) — der System-Player öffnet ihn",
                             path=os.path.relpath(path, HERE), hint=hint) if opened
                          else tr("WAV → {path} ({hint}) — der Player ließ sich nicht automatisch öffnen; "
                                  "Datei manuell öffnen", path=os.path.relpath(path, HERE), hint=hint))

    def write_json(self):
        from arcpak import audio
        path = audio.write_catalog(self.catalogue or [], str(HERE / "out" / "audio" / "katalog.json"))
        self.step.setText(tr("Katalog geschrieben → {path}", path=os.path.relpath(path, HERE)))

    def open_folder(self):
        from arcpak.config import open_folder
        open_folder(HERE / "out" / "audio")
