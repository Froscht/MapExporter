"""GUI tab "Audio": browse the cooked sounds and export them. Game data (arcpak.game /
arcpak.audio) is loaded in a background thread the first time the tab is opened.

Two formats live in the game data: raw 16-bit PCM (exports as playable WAV) and RAD BinkA blocks
(re-embedded into a UEBA container). "▶ Anhören" decodes BinkA through an external FFmpeg; MapExporter
itself has no BinkA decoder. Which FFmpeg build can do it is not written in any version number, so
the tab asks arcpak.audio to probe the installed builds with a real sound and shows the result.
"""
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


def preview_path(name: str) -> str:
    """WAV path for a decoded preview — the same stable file per sound that `audio play` writes, so
    the tab and the command line leave one overwritable WAV behind instead of temp-file litter."""
    from arcpak.audio import preview_wav
    return preview_wav(name)


def play_line(path: str, way: str, info: dict) -> str:
    """One status line describing a decoded sound and how it was started (Audio tab and file
    dialog show the same thing)."""
    hint = tr("BinkA über FFmpeg dekodiert") if way == "ffmpeg" else tr("Roh-PCM")
    peak = f"{info['peak']:.2f}" if info.get("peak") is not None else "?"
    player = info.get("player") or tr("System-Player")
    shown = os.path.relpath(path, HERE)
    if shown.startswith(".." + os.sep):           # the preview WAV sits in the system temp folder
        shown = os.path.basename(path)
    line = (tr("{dauer:.2f} s · {rate} Hz · {kanal} ch · Peak {peak} · {hint} · {player} öffnet "
               "{path}", dauer=info.get("dauer") or 0, rate=info.get("rate") or "?",
               kanal=info.get("kanal") or "?", peak=peak, hint=hint, player=player,
               path=shown))
    if info.get("player_error"):
        line += tr(" — Abspieler meldete: {err}", err=info["player_error"])
    return line


class CatalogueWorker(QThread):
    done = pyqtSignal(object, str)              # catalogue, error

    def run(self):
        try:
            from arcpak import audio
            self.done.emit(audio.catalogue(), "")
        except Exception as e:
            self.done.emit(None, f"{type(e).__name__}: {e}")


class FfmpegWorker(QThread):
    """Ask which FFmpeg build on this machine can decode a UEBA container. Started after the
    catalogue: the probe decodes a real sound and must not race the container mounting."""
    done = pyqtSignal(object)                   # arcpak.audio.ffmpeg_status() dict

    def run(self):
        from arcpak import audio
        try:
            self.done.emit(audio.ffmpeg_status())
        except Exception as e:
            self.done.emit({"exe": "", "capable": False, "version": "", "candidates": [], "tried": [],
                            "hint": f"{type(e).__name__}: {e}"})


class ExportWorker(QThread):
    progress = pyqtSignal(int, int)             # done, total
    note = pyqtSignal(str)                      # why a WAV attempt was given up on (FFmpeg)
    done = pyqtSignal(int, int, int, int, int, int, float)      # wav, binka, raw, failed, bytes, skipped, seconds

    def __init__(self, parent, items, out_dir: str, raw: bool, wav: bool = False):
        super().__init__(parent)
        self.items, self.out_dir, self.raw, self.wav, self.stop = items, out_dir, raw, wav, False

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
                elif self.wav:
                    # BinkA as WAV needs an FFmpeg that reads UEBA; one failure means the build
                    # cannot do it at all, so stop trying and keep writing UEBA containers
                    try:
                        data, _info = audio.bink_wav(it.gpath)
                    except (RuntimeError, ValueError) as e:
                        self.wav = False
                        self.note.emit(str(e))
                        path, kind = audio.export_binka(it, audio.out_path(it, ".binka", self.out_dir)), "binka"
                    else:
                        path = audio.out_path(it, ".wav", self.out_dir)
                        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                        with open(path, "wb") as f:
                            f.write(data)
                        kind = "wav"
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
    """Prepare one sound as a WAV and start a player for it: raw PCM directly, BinkA through the
    external FFmpeg that arcpak.audio probed. done -> (WAV path "" on error, error "" = ok, way
    'pcm'/'ffmpeg', info dict: dauer/rate/kanal/peak/rms/player/player_error)."""
    done = pyqtSignal(str, str, str, object)

    def __init__(self, parent, item, ffmpeg_exe: str = ""):
        super().__init__(parent)
        self.item, self.ffmpeg_exe = item, ffmpeg_exe

    def run(self):
        from arcpak import audio
        try:
            data, way = audio.playback_wav(self.item.gpath)
        except Exception as e:
            self.done.emit("", f"{type(e).__name__}: {e}", "", {})
            return
        path = preview_path(self.item.name)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
        except OSError as e:
            self.done.emit("", f"{type(e).__name__}: {e}", way, {})
            return
        try:
            info = audio.wav_stats(data)
        except Exception:
            info = {"dauer": self.item.duration or 0, "rate": self.item.rate, "kanal": self.item.channels}
        info["bytes"] = len(data)
        try:
            player, _proc = audio.play_wav(path, self.ffmpeg_exe or audio.ffmpeg_exe())
        except Exception as e:
            player, info["player_error"] = "", f"{type(e).__name__}: {e}"
        info["player"] = player
        self.done.emit(path, "", way, info)


class AudioTab(QWidget):
    def __init__(self, parent=None, state=None):
        super().__init__(parent)
        self.catalogue = None
        self.current = None
        self.loader = self.worker = self.player = self.probe = None
        self.ffmpeg = {}                        # arcpak.audio.ffmpeg_status()
        self.wav_hint = ""                      # why a "BinkA als WAV" run fell back to UEBA

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
                             "BinkA als WAV und ▶ Anhören übersetzt ein externes FFmpeg — welcher Build das kann, "
                             "steht nicht in einer Versionsnummer, deshalb wird er hier an einem echten Klang getestet."))
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: #9aa0a6;")
        self.ffmpeg_line = QLabel(tr("FFmpeg-Status wird geprüft …"))
        self.ffmpeg_line.setWordWrap(True)
        self.ffmpeg_line.setStyleSheet("color: #9aa0a6; font-style: italic;")

        self.opt_raw = QCheckBox(tr("unveränderte .ubulk"))
        self.opt_raw.setToolTip(tr("Die Klangdatei byte-genau kopieren, statt den Container neu aufzubauen"))
        self.opt_wav = QCheckBox(tr("BinkA als WAV"))
        self.opt_wav.setToolTip(tr("BinkA-Blöcke mit dem gefundenen FFmpeg zu WAV dekodieren (braucht einen Build, "
                                   "der UEBA lesen kann — siehe Status oben; sonst bleibt es bei .binka)"))
        self.btn_ffmpeg = QPushButton(tr("FFmpeg-Status"))
        self.btn_ffmpeg.setToolTip(tr("Welcher FFmpeg-Build hier BinkA dekodiert, neu prüfen und alle Kandidaten "
                                      "zeigen (Pfad einstellbar in den Einstellungen oder über ARC_FFMPEG)"))
        self.btn_ffmpeg.clicked.connect(self.show_ffmpeg)
        btns = QHBoxLayout()
        self.btn_export = QPushButton(tr("Exportieren"))
        self.btn_export.setStyleSheet("font-weight: bold; padding: 6px 18px;")
        self.btn_export.clicked.connect(lambda: self.export([self.current] if self.current else []))
        self.btn_play = QPushButton(tr("▶ Anhören"))
        self.btn_play.setToolTip(tr("Roh-PCM sofort; BinkA über das externe FFmpeg — welcher Build das kann, "
                                    "steht unten bei den Hinweisen"))
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
                  self.opt_raw, self.opt_wav, self.btn_json, self.btn_folder, self.btn_ffmpeg):
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
        right.addWidget(self.ffmpeg_line)
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
        for w in (self.worker, self.player, self.loader, self.probe):
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
        if not self.ffmpeg:
            self.ensure_ffmpeg()          # probe after the catalogue: it decodes a real sound

    # ---------- FFmpeg (external BinkA decoder) ----------
    def ensure_ffmpeg(self):
        """Probe the installed FFmpeg builds in the background and show what they can do."""
        if self.probe is not None and self.probe.isRunning():
            return
        self.ffmpeg_line.setText(tr("FFmpeg-Status wird geprüft …"))
        self.probe = FfmpegWorker(self)
        self.probe.done.connect(self._ffmpeg_ready)
        self.probe.start()

    def _ffmpeg_ready(self, st):
        self.ffmpeg = st or {}
        if st.get("capable"):
            short = (st.get("version") or "?").split(" Copyright")[0].replace("ffmpeg version ", "")
            self.ffmpeg_line.setText(tr("FFmpeg {version} dekodiert BinkA — ▶ Anhören und „BinkA als WAV“ "
                                        "funktionieren ({path})", version=short, path=st.get("exe") or ""))
            self.ffmpeg_line.setStyleSheet("color: #7c9; font-style: italic;")
        else:
            self.ffmpeg_line.setText(tr("Kein FFmpeg hier kann BinkA dekodieren — {hint}",
                                        hint=st.get("hint") or tr("kein Build gefunden")))
            self.ffmpeg_line.setStyleSheet("color: #e86; font-style: italic;")
        self.btn_play.setEnabled(self.current is not None and self.current.ubulk)

    def show_ffmpeg(self):
        """Detail view of the probe: every build that was found and what it can do."""
        if not self.ffmpeg:
            self.ensure_ffmpeg()
            return
        st = self.ffmpeg
        lines = [tr("benutzt: {path}", path=st.get("exe") or tr("— kein FFmpeg gefunden")),
                 tr("Version: {v}", v=st.get("version") or "—"),
                 tr("BinkA (UEBA): {k}", k=tr("JA — dekodiert und als WAV brauchbar") if st.get("capable")
                   else tr("NEIN — {hint}", hint=st.get("hint") or "")),
                 ""]
        lines += [tr("geprüft: {exe} → {r}", exe=exe, r=tr("kann BinkA") if ok else tr("kann BinkA nicht"))
                  for exe, ok in st.get("tried", [])]
        rest = st.get("candidates", [])[len(st.get("tried", [])):] 
        if rest:
            lines.append(tr("nicht geprüft (der erste passende reicht): {p}", p="\n".join(rest)))
        lines.append(tr("Pfad vorgeben: ARC_FFMPEG=/pfad/zu/ffmpeg oder in den Einstellungen"))
        QMessageBox.information(self, tr("FFmpeg"), "\n".join(lines))

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
        self.worker = ExportWorker(self, jobs, out_dir, self.opt_raw.isChecked(), self.opt_wav.isChecked())
        self.worker.progress.connect(lambda d, t: self.progress.setValue(int(1000 * d / max(t, 1))))
        self.worker.note.connect(lambda msg: setattr(self, "wav_hint", msg))
        self.worker.done.connect(self._export_done)
        self.worker.start()

    def _export_done(self, wav, binka, raw, fails, total, skipped, secs):
        self.btn_export.setEnabled(self.current is not None and self.current.ubulk)
        self.btn_all.setEnabled(True); self.btn_cancel.setEnabled(False)
        self.progress.setVisible(False)
        parts = [f"{n} {k}" for n, k in ((wav, "WAV"), (binka, "UEBA"), (raw, ".ubulk")) if n]
        hint = getattr(self, "wav_hint", "")
        self.step.setText(tr("{parts} → {dir} ({size} MB, {sec:.0f} s)", parts=", ".join(parts) or "—",
                             dir=os.path.relpath(HERE / "out" / "audio", HERE), size=f"{total / 1e6:.1f}", sec=secs)
                          + (tr(", {n} Fehler", n=fails) if fails else "")
                          + (tr(", {n} ohne Klangdaten übersprungen", n=skipped) if skipped else "")
                          + (tr(" — WAV abgebrochen: {hint}", hint=hint) if hint else ""))
        self.wav_hint = ""

    # ---------- playback ----------
    def play(self):
        if self.current is None or not self.current.ubulk or (self.player and self.player.isRunning()):
            return
        self.btn_play.setEnabled(False)
        self.step.setText(tr("Klang wird vorbereitet …"))
        self.player = PlayWorker(self, self.current, (self.ffmpeg or {}).get("exe", ""))
        self.player.done.connect(self._play_done)
        self.player.start()

    def _play_done(self, path, err, way, info):
        self.btn_play.setEnabled(self.current is not None and self.current.ubulk)
        if err:
            self.step.setText(tr("Abspielen nicht möglich: {err}", err=err))
            return
        if not (info or {}).get("player"):        # no ffplay/paplay here: ask the desktop
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        self.step.setText(play_line(path, way, info or {}))

    def write_json(self):
        from arcpak import audio
        path = audio.write_catalog(self.catalogue or [], str(HERE / "out" / "audio" / "katalog.json"))
        self.step.setText(tr("Katalog geschrieben → {path}", path=os.path.relpath(path, HERE)))

    def open_folder(self):
        from arcpak.config import open_folder
        open_folder(HERE / "out" / "audio")
