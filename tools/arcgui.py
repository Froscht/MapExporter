#!/usr/bin/env python3
"""ARC Raiders - asset and map workbench (PyQt6).

Tabs:
  Browser   - browse and extract the mounted .utoc/.ucas containers
  Maps      - pick a map -> export options -> export: chain P2 (placements) ->
              P2b (play area, if needed) -> P3 (GLB) -> simplify (Blender) ->
              open in Blender; each step runs as a subprocess, output shown live in the log
  Modelle   - browse weapons/bots/gear/outfits, 3D preview, export GLB
  Audio     - browse the cooked sounds (duration, rate, channels), export WAV (raw PCM) or
              re-embed the BinkA blocks into a UEBA container
  Texturen  - browse the cooked Texture2D assets, preview them, export PNG
  Inventory - what already exists: extracted/, out/ with file/byte counts

Keys/paths live in arcpak/settings.json -> edit via Settings dialog when
the game patches its encryption.
"""
import sys
import os
import re
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from PyQt6.QtCore import Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QDesktopServices, QFont, QPixmap
from PyQt6.QtWidgets import (QSizePolicy, QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
                             QFormLayout, QGroupBox, QHBoxLayout,
                             QHeaderView, QLineEdit, QListWidget, QMainWindow, QMessageBox,
                             QPlainTextEdit, QProgressBar, QProgressDialog, QPushButton, QLabel, QTableWidget,
                             QTableWidgetItem, QTabWidget, QToolBar, QTreeWidget,
                             QListWidgetItem, QTreeWidgetItem, QVBoxLayout, QWidget)

from arcpak.config import Config
from arcpak.config import detached_kwargs, find_blender, open_folder, parse_progress
from arcpak.iostore import mount_directory
from arcpak.mapnames import map_names
from i18n import LANGUAGES, set_language, tr
from audio_tab import AudioTab, PlayWorker
from models_tab import ModelsTab
from textures_tab import ImageWorker, TexturesTab

def dauer_text(sek: float) -> str:
    """42 s · 3:07 · 1:02:33"""
    sek = int(round(sek))
    if sek < 60:
        return f"{sek} s"
    h, rest = divmod(sek, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


EXT_COLORS = {".uasset": "#22aa44", ".ubulk": "#cc6622", ".upkg": "#2266cc", ".uexp": "#2266cc",
              ".utoc": "#888888", ".ini": "#aa22aa"}


class ExtractWorker(QThread):
    progress = pyqtSignal(int, int, str)          # done, total, current path
    finished_n = pyqtSignal(int, int)             # ok, failed

    def __init__(self, parent, containers, jobs, out_dir):
        super().__init__(parent)
        self.containers, self.jobs, self.out_dir = containers, jobs, out_dir

    def run(self):
        ok = fail = 0
        for i, (ci, ud, relpath) in enumerate(self.jobs):
            try:
                data = self.containers[ci].get_chunk(ud)
                target = self.out_dir / relpath
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                ok += 1
            except Exception:
                fail += 1
            self.progress.emit(i + 1, len(self.jobs), relpath)
        self.finished_n.emit(ok, fail)


class ChainWorker(QThread):
    """Run several tools one after another; abort on the first failure.

    step = (title, cmd) or (title, cmd, {"skip_exit": code, "skip_flags": {...}, "skip_text": ...}):
    if the step exits with skip_exit it is skipped instead of aborting the chain, and skip_flags
    are removed from all following commands (e.g. --crop when there is no play area)."""
    line = pyqtSignal(str)
    step = pyqtSignal(int, int, str)             # no., count, title
    progress = pyqtSignal(float)              # 0..1 within the current step
    done = pyqtSignal(int, float)                # exit code, total duration in seconds

    def __init__(self, parent, steps):
        super().__init__(parent)
        self.steps, self.proc, self.stop = steps, None, False

    def run(self):
        rc = 0
        t_start = time.monotonic()
        weg = set()                              # flags dropped after a skipped step
        for i, (title, cmd, *rest) in enumerate(self.steps):
            opt = rest[0] if rest else {}
            if self.stop:
                rc = -1
                break
            cmd = [a for a in cmd if a not in weg]
            self.step.emit(i + 1, len(self.steps), title)
            t_step = time.monotonic()
            self.line.emit(f"\n=== [{i + 1}/{len(self.steps)}] {title}\n$ " + " ".join(cmd))
            if cmd[0] == find_blender() and "--background" not in cmd:
                # start GUI Blender detached - the chain does not wait for the window
                subprocess.Popen(cmd, cwd=str(HERE), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 **detached_kwargs())
                self.line.emit(tr("Blender gestartet (eigenes Fenster)."))
                continue
            env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
            self.proc = subprocess.Popen(cmd, cwd=str(HERE), stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True, bufsize=1,
                                         encoding="utf-8", errors="replace", env=env)
            for l in self.proc.stdout:
                l = l.rstrip()
                fp = parse_progress(l)
                if fp:
                    self.progress.emit(min(1.0, fp[0] / fp[1]))
                    continue
                if l.startswith(("building schema", "schema union")) or "| INFO: Blender create" in l:
                    continue
                self.line.emit(l)
            self.proc.wait()
            rc = self.proc.returncode
            if opt and rc == opt.get("skip_exit"):
                weg |= set(opt.get("skip_flags", ()))
                self.progress.emit(1.0)
                self.line.emit("--- " + opt.get("skip_text", title))
                rc = 0
                continue
            if rc == 0:
                self.progress.emit(1.0)
                self.line.emit(tr("--- Schritt {i} fertig nach {zeit}", i=i + 1,
                                  zeit=dauer_text(time.monotonic() - t_step)))
            if rc != 0:
                self.line.emit(tr("!!! Schritt {i} ({title}) endete mit exit {rc} — Kette abgebrochen",
                                  i=i + 1, title=title, rc=rc))
                break
        self.done.emit(rc, time.monotonic() - t_start)

    def cancel(self):
        self.stop = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


# ---------- file info (browser double-click) ---------------------------------------------------
def game_gpath(path: str):
    """/Game path of a FULL container entry path, or None (engine mounts are not addressable).

    Every game mount starts with "../../.." and the index keys carry the whole path — a bare
    container-relative path is not enough, the caller must prepend the container's mount."""
    from arcpak import models
    content = models.CONTENT                                   # "../../../PioneerGame/Content/"
    if path[:len(content)].lower() != content.lower():
        return None
    gpath = models.to_gpath(content + path[len(content):])
    return gpath if models.exists(gpath) else None


class InfoWorker(QThread):
    """Classify one browser file and collect its package details (schema reads off the UI thread).

    .ubulk/.uexp/.upkg are judged through their .uasset counterpart; kind:
    'map' | 'textur' | 'mesh' | 'klang' | 'uasset' | 'fremd' | 'fehler'."""
    done = pyqtSignal(str, object, str)                # kind, data dict, error

    def __init__(self, parent, rel):
        super().__init__(parent)
        self.rel = rel

    def run(self):
        from arcpak import game, models
        try:
            ext = os.path.splitext(self.rel)[1].lower()
            base, data = self.rel, {}
            if ext in (".ubulk", ".uexp", ".upkg"):
                base = self.rel[:-len(ext)] + ".uasset"
                data["companion"] = ext
            if os.path.splitext(base)[1].lower() == ".umap":
                m = re.search(r"Maps/([^/]+)/", self.rel)
                data["map"] = m.group(1) if m else os.path.splitext(self.rel.rpartition("/")[2])[0]
                self.done.emit("map", data, "")
                return
            gpath = game_gpath(base)
            if gpath is None:
                self.done.emit("fremd", data, "")
                return
            data["gpath"] = gpath
            stem = os.path.splitext(base.rpartition("/")[2])[0]
            _raw, pkg = models.package(gpath)
            exports = [(pkg.export_class(e, script_names=game.smap) or "?", e.size) for e in pkg.exports]
            data["exports"] = exports
            classes = {c for c, _ in exports}
            key = models.to_key(gpath)
            if "Texture2D" in classes:
                from arcpak import texture
                data["item"] = texture.Item(name=stem, gpath=gpath, folder=texture._folder(key))
                self.done.emit("textur", data, "")
            elif classes & {"StaticMesh", "SkeletalMesh"}:
                data["mesh_class"] = "SkeletalMesh" if "SkeletalMesh" in classes else "StaticMesh"
                self.done.emit("mesh", data, "")
            elif any(c and "Sound" in c for c in classes) or "/Audio/" in gpath:
                from arcpak import audio
                meta = audio._sound_wave(gpath)
                hits = models.lookup(models.to_key(gpath, ".ubulk"))
                ubytes = int(hits[0][0].chunk_offsets[hits[0][1]][1]) if hits else 0
                it = audio.Item(name=stem, gpath=gpath, folder="",
                                duration=(meta[0] if meta else None), rate=(meta[1] if meta else None),
                                channels=(meta[2] if meta else None), size=ubytes,
                                ubulk=bool(hits), pcm=bool(hits) and audio._pcm_shape(gpath, ubytes, meta) is not None)
                data["item"] = it
                self.done.emit("klang", data, "")
            else:
                self.done.emit("uasset", data, "")
        except Exception as e:
            self.done.emit("fehler", {}, f"{type(e).__name__}: {e}")


class GlbWorker(QThread):
    """Decode one browser mesh directly to a GLB (out/models/Browser/), textures included."""
    done = pyqtSignal(str, object, str)                # path, warnings, error

    def __init__(self, parent, item):
        super().__init__(parent)
        self.item = item

    def run(self):
        from arcpak import models
        try:
            meshes, warnings = models.build(self.item)
            if not meshes:
                raise ValueError("keine Meshes im Paket")
            warnings += models.apply_textures(meshes)
            path = models.out_path(self.item)
            models.export_glb(meshes, path)
        except Exception as e:
            self.done.emit("", [], f"{type(e).__name__}: {e}")
            return
        self.done.emit(path, warnings, "")


class FileInfoDialog(QDialog):
    """Everything one browser file offers (double-click): texture preview + PNG, sound play/export,
    mesh as GLB, map → Karten tab, generic package → export class list."""

    def __init__(self, win, ci, ud, rel):
        super().__init__(win)
        self.win, self.ci, self.ud, self.rel = win, ci, ud, rel
        self.kind, self.data = "", {}
        self.info_worker = self.image_worker = self.play_worker = self.glb_worker = None
        c = win.containers[ci]
        fname = rel.rpartition("/")[2]
        self.setWindowTitle(tr("Dateiinfo — {name}", name=fname))
        self.resize(760, 600)
        lay = QVBoxLayout(self)

        head = QLabel(f"<b>{fname}</b> — {tr('{mb} MB komprimiert, Container {container}',
                                             mb=f'{c.chunk_offsets[ud][1] / 1e6:.2f}', container=c.base)}")
        head.setWordWrap(True)
        head.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        path_line = QLabel(f"<code>{rel}</code>")
        path_line.setWordWrap(True)
        path_line.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.info = QLabel(tr("Untersuche die Datei …"))
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(head)
        lay.addWidget(path_line)
        lay.addWidget(self.info)

        self.preview = QLabel("—")
        self.preview.setMinimumSize(320, 260)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setVisible(False)
        lay.addWidget(self.preview, 1)

        btns = QHBoxLayout()
        self.btn_play = QPushButton(tr("▶ Anhören"))
        self.btn_play.clicked.connect(self.play)
        self.btn_wav = QPushButton(tr("Als WAV"))
        self.btn_wav.clicked.connect(lambda: self._sound_export("wav"))
        self.btn_binka = QPushButton(tr("Als UEBA (.binka)"))
        self.btn_binka.clicked.connect(lambda: self._sound_export("binka"))
        self.btn_png = QPushButton(tr("Als PNG exportieren"))
        self.btn_png.clicked.connect(self._export_png)
        self.btn_glb = QPushButton(tr("Als GLB exportieren"))
        self.btn_glb.clicked.connect(self._export_glb)
        self.btn_maps = QPushButton(tr("Zum Karten-Tab"))
        self.btn_maps.clicked.connect(self._goto_maps_tab)
        self.btn_raw = QPushButton(tr("Rohe Datei extrahieren"))
        self.btn_raw.setToolTip(tr("Die Containerdatei unverändert in den Ausgabearchiv-Ordner schreiben"))
        self.btn_raw.clicked.connect(lambda: self.win._run_extract([(self.ci, self.ud, self.rel)]))
        for b in (self.btn_play, self.btn_wav, self.btn_binka, self.btn_png, self.btn_glb, self.btn_maps):
            b.setVisible(False)
            btns.addWidget(b)
        btns.addWidget(self.btn_raw)
        btns.addStretch(1)
        closer = QPushButton(tr("Schließen"))
        closer.clicked.connect(self.accept)
        btns.addWidget(closer)
        lay.addLayout(btns)
        self.step = QLabel("")
        self.step.setWordWrap(True)
        self.step.setStyleSheet("color: #9aa0a6;")
        lay.addWidget(self.step)

        # classification works on the full entry path (mount + relative), not on the display rel
        self.info_worker = InfoWorker(self, f"{c.mount_point.rstrip('/')}/{rel}")
        self.info_worker.done.connect(self._kind_done)
        self.info_worker.start()

    # ---------- classification ----------
    def _kind_done(self, kind, data, err):
        self.kind, self.data = kind, data or {}
        if err:
            self.info.setText(f"<span style='color:#e88'>{err}</span>")
            return
        lines = []
        if self.data.get("companion"):
            lines.append(tr("Diese {ext} gehört zum gleichnamigen .uasset — die Infos unten beziehen sich darauf.",
                            ext=self.data["companion"]))
        gpath = self.data.get("gpath") or ""
        if kind == "map":
            name = self.data.get("map") or ""
            disp = self.win.map_names.get(name)
            lines.append(f"<b>{tr('Karte')}</b> {name}" + (f" — {disp}" if disp else ""))
            lines.append(tr("Karten exportierst du im Tab Karten (Platzierungen, Spielbereich, GLB)."))
            self.btn_maps.setVisible(True)
        elif kind == "textur":
            it = self.data["item"]
            lines.append(f"<b>{tr('Textur')}</b> <code>{gpath}</code>")
            self.preview.setVisible(True)
            self.step.setText(tr("Vorschau wird geladen …"))
            self.image_worker = ImageWorker(self, it, 1024, False)
            self.image_worker.done.connect(self._preview_done)
            self.image_worker.start()
            self.btn_png.setVisible(True)
        elif kind == "klang":
            it = self.data["item"]
            dauer = dauer_text(it.duration) if it.duration else tr("unbekannt")
            fmt = (tr("Roh-PCM (16-bit) → WAV") if it.pcm
                   else (tr("BinkA → UEBA-Container; ▶ dekodiert über FFmpeg") if it.ubulk
                         else tr("nur Meta-Daten")))
            lines.append(f"<b>{tr('Klang')}</b> <code>{gpath}</code><br>"
                         f"{dauer} · {it.rate or '?'} Hz · {it.channels or '?'} Kanäle · {fmt}")
            if it.ubulk:
                self.btn_play.setVisible(True)
                (self.btn_wav if it.pcm else self.btn_binka).setVisible(True)
        elif kind == "mesh":
            lines.append(f"<b>{self.data.get('mesh_class')}</b> <code>{gpath}</code>")
            lines.append(tr("Models mit Vorschau gibt es im Tab Modelle — hier geht der Weg direkt zum GLB."))
            self.btn_glb.setVisible(True)
        elif kind == "uasset":
            exports = self.data.get("exports") or []
            cls = ", ".join(f"{c} ({sz:,} B)" for c, sz in exports[:12]) + (" …" if len(exports) > 12 else "")
            lines.append(f"<b>{tr('Paket')}</b> <code>{gpath}</code>")
            lines.append(tr("Export-Klassen: {cls}", cls=cls or "—"))
        elif kind == "fremd":
            lines.append(tr("Engine-Anteil oder fremder Mount — nicht über /Game adressierbar, "
                            "deshalb nur Container-Info."))
        self.info.setText("<br>".join(lines) or "—")

    # ---------- texture preview / PNG ----------
    def _preview_done(self, item, png, info, err):
        if item is not self.data.get("item"):
            return
        if err or png is None:
            self.preview.setText("—")
            self.step.setText(tr("Keine Vorschau: {err}", err=err))
            return
        pm = QPixmap()
        pm.loadFromData(png)
        self.preview.setPixmap(pm.scaled(640, 480, Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.SmoothTransformation))
        raum = (info or {}).get("farbraum")
        self.step.setText(f"{pm.width()}×{pm.height()}" + (f" · {tr('Farbraum')}: {raum}" if raum else ""))

    def _export_png(self):
        it = self.data.get("item")
        if it is None:
            return
        from arcpak import texture
        try:
            path = texture.export_png(it.gpath, texture.out_path(it, base=str(HERE / "out" / "textures")))
        except Exception as e:
            self.step.setText(tr("PNG-Export fehlgeschlagen: {err}", err=f"{type(e).__name__}: {e}"))
            return
        self.step.setText(tr("PNG geschrieben → {path}", path=os.path.relpath(path, HERE)))

    # ---------- sound ----------
    def play(self):
        it = self.data.get("item")
        if it is None or not it.ubulk or (self.play_worker and self.play_worker.isRunning()):
            return
        self.btn_play.setEnabled(False)
        self.step.setText(tr("Klang wird vorbereitet …"))
        self.play_worker = PlayWorker(self, it)
        self.play_worker.done.connect(self._play_done)
        self.play_worker.start()

    def _play_done(self, path, err):
        way = self.play_worker.way if self.play_worker else ""
        self.btn_play.setEnabled(self.data.get("item") is not None)
        if err:
            self.step.setText(tr("Abspielen nicht möglich: {err}", err=err))
            return
        opened = QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        hint = tr("BinkA über FFmpeg dekodiert") if way == "ffmpeg" else tr("Roh-PCM")
        self.step.setText(tr("WAV → {path} ({hint}) — {action}", path=os.path.basename(path), hint=hint,
                             action=tr("der System-Player öffnet ihn") if opened
                             else tr("den Player konnte man nicht automatisch öffnen — Datei manuell öffnen")))

    def _sound_export(self, kind):
        it = self.data.get("item")
        if it is None:
            return
        from arcpak import audio
        out = str(HERE / "out" / "audio")
        try:
            if kind == "wav":
                path = audio.export_wav(it, audio.out_path(it, ".wav", out))
            else:
                path = audio.export_binka(it, audio.out_path(it, ".binka", out))
        except Exception as e:
            self.step.setText(tr("Export fehlgeschlagen: {err}", err=f"{type(e).__name__}: {e}"))
            return
        self.step.setText(tr("{ext} geschrieben → {path}", ext=os.path.splitext(path)[1].lstrip(".").upper(),
                             path=os.path.relpath(path, HERE)))

    # ---------- mesh ----------
    def _export_glb(self):
        gpath = self.data.get("gpath")
        if not gpath or (self.glb_worker and self.glb_worker.isRunning()):
            return
        from arcpak import models
        item = models.Item("Browser", os.path.splitext(self.rel.rpartition("/")[2])[0], gpath, "")
        self.btn_glb.setEnabled(False)
        self.step.setText(tr("Dekodiere Mesh …"))
        self.glb_worker = GlbWorker(self, item)
        self.glb_worker.done.connect(self._glb_done)
        self.glb_worker.start()

    def _glb_done(self, path, warnings, err):
        self.btn_glb.setEnabled(True)
        if err:
            self.step.setText(tr("GLB-Export fehlgeschlagen: {err}", err=err))
            return
        warn = tr(" · {n} Warnungen", n=len(warnings)) if warnings else ""
        self.step.setText(tr("GLB geschrieben → {path}{warn}", path=os.path.relpath(path, HERE), warn=warn))

    # ---------- map ----------
    def _goto_maps_tab(self):
        name = self.data.get("map") or ""
        self.win.tabs.setCurrentWidget(self.win.maps_tab)
        for i in range(self.win.map_list.count()):
            if self.win.map_list.item(i).data(Qt.ItemDataRole.UserRole) == name:
                self.win.map_list.setCurrentRow(i)
                break
        self.accept()

    def closeEvent(self, ev):
        for w in (self.info_worker, self.image_worker, self.play_worker, self.glb_worker):
            if w is not None and w.isRunning():
                try:
                    w.done.disconnect()
                except (RuntimeError, TypeError):
                    pass
        super().closeEvent(ev)


class MainWindow(QMainWindow):
    def __init__(self, state=None):
        """state: data kept from the previous window when the UI is rebuilt for a new language
        (mounted containers, model catalogue), so the rebuild does not reload everything."""
        super().__init__()
        state = state or {}
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.config = Config.load()
        set_language(self.config.language)
        self.setWindowTitle(tr("ARC Raiders — Asset- und Karten-Werkzeugbank"))
        self.resize(1280, 780)
        self.containers = []
        self.worker = None
        self.map_names = {}

        tb = QToolBar("main")
        tb.setMovable(False)
        self.addToolBar(tb)
        for label, slot in ((tr("Neu laden"), self.reload), (tr("Einstellungen"), self.open_settings)):
            act = QPushButton(label)
            act.clicked.connect(slot)
            tb.addWidget(act)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        tb.addWidget(QLabel(tr("Sprache:") + " "))
        self.lang_box = QComboBox()
        for code, name in LANGUAGES.items():
            self.lang_box.addItem(name, code)
        self.lang_box.setCurrentIndex(list(LANGUAGES).index(set_language(self.config.language)))
        self.lang_box.currentIndexChanged.connect(self.change_language)
        tb.addWidget(self.lang_box)

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.tabs.addTab(self.make_browser_tab(), tr("Browser"))
        self.maps_tab = self.make_maps_tab()
        self.tabs.addTab(self.maps_tab, tr("Karten"))
        self.models_tab = ModelsTab(self, state.get("models"))
        self.tabs.addTab(self.models_tab, tr("Modelle"))
        self.audio_tab = AudioTab(self, state.get("audio"))
        self.tabs.addTab(self.audio_tab, tr("Audio"))
        self.textures_tab = TexturesTab(self, state.get("textures"))
        self.tabs.addTab(self.textures_tab, tr("Texturen"))
        self.tabs.currentChanged.connect(self.tab_shown)
        self.tabs.addTab(self.make_inventory_tab(), tr("Bestand"))
        self.status = self.statusBar()
        self.reload(state.get("containers"))

    def tab_shown(self, index):
        """Tabs load their (heavy) catalogue in a background thread the first time they appear."""
        w = self.tabs.widget(index)
        for tab in (self.models_tab, self.audio_tab, self.textures_tab):
            if w is tab:
                tab.ensure_loaded()

    # ---------- Browser tab ----------
    def make_browser_tab(self):
        w = QWidget()
        top = QHBoxLayout()
        for label, slot in ((tr("Auswahl extrahieren"), self.extract_selected),
                            (tr("Container extrahieren"), self.extract_container),
                            (tr("Liste speichern"), self.save_list)):
            b = QPushButton(label); b.clicked.connect(slot); top.addWidget(b)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Nach Pfad filtern…"))
        self.search.setMinimumWidth(320)
        self.search.textChanged.connect(self.build_tree)
        top.addWidget(self.search)
        self.container_box = QListWidget()
        self.container_box.setMaximumWidth(280)
        self.container_box.currentRowChanged.connect(self.build_tree)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels([tr("Pfad"), tr("Größe"), tr("Typ"), tr("Container")])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.setUniformRowHeights(True)
        self.tree.itemDoubleClicked.connect(self.show_file_info)
        self.tree.setToolTip(tr("Doppelklick auf eine Datei: Vorschau, Abspielen, Export, Details"))
        spl = QHBoxLayout()
        spl.addWidget(self.container_box)
        spl.addWidget(self.tree, 1)
        lay = QVBoxLayout(w)
        lay.addLayout(top)
        lay.addLayout(spl)
        return w

    # ---------- mounting / tree ----------
    def reload(self, containers=None):
        if containers is None:
            for c in self.containers:
                c.close()
            containers = mount_directory(self.config.pak_dir, self.config.game)
        self.containers = containers
        self.container_box.blockSignals(True)
        self.container_box.clear()
        for c in self.containers:
            self.container_box.addItem(f"{c.base}  ({len(c.entries)})")
        self.status.showMessage(tr("{n} Container, {files} Dateien  —  {dir}", n=len(self.containers),
                                   files=sum(len(c.entries) for c in self.containers), dir=self.config.pak_dir))
        if self.containers and self.container_box.currentRow() < 0:
            biggest = max(range(len(self.containers)), key=lambda i: len(self.containers[i].entries))
            self.container_box.setCurrentRow(biggest)
        self.container_box.blockSignals(False)
        self.build_tree()
        self._map_counts = None
        self.load_map_names()
        self.build_map_list()

    def build_tree(self):
        ci = self.container_box.currentRow()
        self.tree.clear()
        if ci < 0 or ci >= len(self.containers):
            return
        c = self.containers[ci]
        flt = self.search.text().strip().lower()
        # Build the items detached from the view and insert them in one call: inserting
        # 80 000 items one by one into a visible tree takes seconds.
        folders, tops, brushes = {}, [], {}
        mount = c.mount_point.rstrip("/")
        added = 0
        for path, ud in c.entries:
            if flt and flt not in path.lower():
                continue
            rel = path.replace(mount, "").lstrip("/") or path
            folder, _, fname = rel.rpartition("/")
            parent = None
            if folder:
                parent = folders.get(folder)
                if parent is None:
                    prefix = ""
                    for part in folder.split("/"):
                        prefix = f"{prefix}/{part}" if prefix else part
                        node = folders.get(prefix)
                        if node is None:
                            node = QTreeWidgetItem([part, "", "", c.base])
                            (parent.addChild(node) if parent is not None else tops.append(node))
                            folders[prefix] = node
                        parent = node
            ext = os.path.splitext(fname)[1].lower()
            item = QTreeWidgetItem([fname, f"{c.chunk_offsets[ud][1]:,}", ext or "file", c.base])
            color = EXT_COLORS.get(ext)
            if color:
                if color not in brushes:
                    brushes[color] = QBrush(QColor(color))
                item.setForeground(2, brushes[color])
            item.setData(0, Qt.ItemDataRole.UserRole, (ci, ud, rel))
            (parent.addChild(item) if parent is not None else tops.append(item))
            added += 1
        self.tree.setUpdatesEnabled(False)
        self.tree.addTopLevelItems(tops)
        self.tree.setUpdatesEnabled(True)
        top = self.tree.topLevelItem(0)
        if top:
            top.setExpanded(True)
        self.status.showMessage(tr("Container {name}: {shown}/{total} Dateien angezeigt",
                                   name=c.base, shown=added, total=len(c.entries)))

    # ---------- file info ----------
    def show_file_info(self, item, _column=0):
        """Double-click in the browser: open the per-kind info dialog for that file."""
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data is None:
            return                                  # a folder — nothing to show
        FileInfoDialog(self, *data).exec()

    # ---------- extraction ----------
    def _collect(self, items, jobs, seen):
        for it in items:
            if it.childCount():
                self._collect([it.child(i) for i in range(it.childCount())], jobs, seen)
            else:
                data = it.data(0, Qt.ItemDataRole.UserRole)
                if data and data[2] not in seen:
                    seen.add(data[2])
                    jobs.append(data)

    def extract_selected(self):
        items = self.tree.selectedItems()
        if not items:
            QMessageBox.information(self, tr("Extrahieren"), tr("Erst Dateien oder Ordner im Baum auswählen."))
            return
        jobs, seen = [], set()
        self._collect(items, jobs, seen)
        self._run_extract(jobs)

    def extract_container(self):
        ci = self.container_box.currentRow()
        if ci < 0:
            return
        c = self.containers[ci]
        out = QFileDialog.getExistingDirectory(self, tr("Container extrahieren nach…"), self.config.output_dir)
        if not out:
            return
        jobs = [(ci, ud, p.replace(c.mount_point.rstrip("/"), "").lstrip("/")) for p, ud in c.entries]
        self._run_extract(jobs, Path(out))

    def _run_extract(self, jobs, out_dir=None):
        out_dir = Path(out_dir or (Path(self.config.output_dir) if Path(self.config.output_dir).is_absolute()
                                   else HERE / self.config.output_dir))
        dlg = QProgressDialog(tr("Extrahiere {n} Dateien nach {dir}…", n=len(jobs), dir=out_dir),
                              tr("Abbrechen"), 0, len(jobs), self)
        dlg.setWindowTitle(tr("Extrahieren"))
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        self.worker = ExtractWorker(self, self.containers, jobs, out_dir)
        self.worker.progress.connect(lambda d, t, p: (dlg.setValue(d), dlg.setLabelText(f"{d}/{t}  {p}")))
        self.worker.finished_n.connect(lambda ok, f: dlg.close() or QMessageBox.information(
            self, tr("Extrahieren"), tr("Fertig: {ok} ok, {fail} fehlgeschlagen → {dir}", ok=ok, fail=f, dir=out_dir)))
        self.worker.start()
        dlg.exec()

    def save_list(self):
        out, _ = QFileDialog.getSaveFileName(self, tr("Dateiliste speichern"), "filelist.txt")
        if not out:
            return
        with open(out, "w", encoding="utf-8") as f:
            for c in self.containers:
                for p, ud in c.entries:
                    f.write(f"{c.base}\t{c.chunk_offsets[ud][1]}\t{p}\n")
        self.status.showMessage(tr("Liste gespeichert → {path}", path=out))

    # ---------- Maps tab ----------
    def make_maps_tab(self):
        w = QWidget()
        self.map_search = QLineEdit()
        self.map_search.setPlaceholderText(tr("Karte filtern…"))
        self.map_search.textChanged.connect(self.build_map_list)
        self.map_list = QListWidget()
        self.map_list.setMinimumWidth(420)
        self.map_list.currentRowChanged.connect(self.map_status)
        left = QVBoxLayout()
        left.addWidget(self.map_search)
        left.addWidget(self.map_list, 1)

        self.map_info = QLabel("—")
        self.map_info.setWordWrap(True)
        self.map_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        # export options
        box = QGroupBox(tr("Export-Optionen"))
        form = QVBoxLayout(box)
        def cb(text, on, tip):
            c = QCheckBox(tr(text)); c.setChecked(on); c.setToolTip(tr(tip)); form.addWidget(c); return c
        self.opt_p2 = cb("Platzierungen neu lesen (P2)", True,
                         "nötig nach Spiel-Patch oder Code-Änderung; "
                         "aus = vorhandenes JSONL verwenden")
        self.opt_terrain = cb("Terrain (Landscape)", True, "Landscape-Höhengitter mit exportieren")
        self.opt_crop = cb("Auf Spielbereich zuschneiden", True,
                           "nur was im begehbaren Bereich liegt (BlockingVolumes + Flutfüllung ab PlayerStart); "
                           "entfernt Himmel, Kulissen, Umland")
        self.opt_area = cb("Spielbereich einzeichnen (grüner Teppich + Zaun)", False,
                           "begehbare Fläche als halbtransparente Markierung")
        self.opt_leaves = cb("Baumblätter entfernen", True,
                             "Sektionen mit Branch/Leaf/Foliage/Needle-Material; Stämme bleiben")
        self.opt_effects = cb("Effekt-Meshes entfernen (Rauch, Lichtstrahlen, Laub-Partikel)", True,
                              "Meshes aus /Game/Pioneer/Effects/ — flache Gitter, die erst das VFX-Material "
                              "im Spiel zu Rauch/Licht macht; ohne Material große sinnlose Wände "
                              "(z. B. SM_FX_SmokeplumeGrid in Spaceport)")
        self.opt_helpers = cb("Unsichtbare Hilfsgeometrie entfernen (Licht, Himmel, Occluder, Debug-Blöcke)", True,
                              "Licht-Geometrie und Lichtblocker, Licht-Portale, Himmels- und Wolkenkugeln, Occluder, "
                              "Editor-Meshes, Volumes und Grundformen mit Debug-/Standardmaterial — im Spiel nicht "
                              "sichtbar, im Export große Quader und Platten (z. B. auf dem Startturm in Spaceport)")
        self.opt_decals = cb("Decal-Flächen entfernen (Flecken, Graffiti, Risse)", True,
                             "Decals sind im Spiel Texturen auf Wänden und Böden; der Export hat keine Texturen, "
                             "übrig bleiben graue Flächen knapp vor den Oberflächen")
        rowv = QHBoxLayout()
        self.opt_simplify = QCheckBox(tr("Polygone vereinfachen auf"))
        self.opt_simplify.setChecked(True)
        self.opt_simplify.setToolTip(tr("Blender headless, Decimate Collapse je Mesh — nur der Mesh-Vorrat geht durch Blender"))
        self.opt_ratio = QDoubleSpinBox()
        self.opt_ratio.setRange(0.05, 1.0); self.opt_ratio.setSingleStep(0.05); self.opt_ratio.setValue(0.5)
        self.opt_ratio.setSuffix("  " + tr("(Anteil)"))
        rowv.addWidget(self.opt_simplify); rowv.addWidget(self.opt_ratio); rowv.addStretch(1)
        form.addLayout(rowv)
        self.opt_volumes = cb("Analyse: BlockingVolumes + Limiter einzeichnen", False,
                              "nur zur Raum-Analyse — unsichtbare Wände (orange) und PlayableAreaLimiter (gelb)")
        self.opt_blender = cb("Danach in Blender öffnen", True, "Ergebnis-GLB im Blender-Fenster")
        self.opt_crop.toggled.connect(self.map_status)
        self.opt_simplify.toggled.connect(self.map_status)

        btns = QHBoxLayout()
        self.btn_export = QPushButton(tr("Exportieren"))
        self.btn_export.setStyleSheet("font-weight: bold; padding: 6px 18px;")
        self.btn_export.clicked.connect(self.run_export)
        self.btn_cancel = QPushButton(tr("Abbrechen"))
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(lambda: self.worker and self.worker.cancel())
        self.btn_open = QPushButton(tr("Nur Blender öffnen"))
        self.btn_open.clicked.connect(self.open_blender)
        self.btn_folder = QPushButton(tr("Ordner öffnen"))
        self.btn_folder.clicked.connect(lambda: open_folder(HERE / "out" / "map"))
        for b in (self.btn_export, self.btn_cancel, self.btn_open, self.btn_folder):
            btns.addWidget(b)
        btns.addStretch(1)
        self.map_step = QLabel("")
        btns.addWidget(self.map_step)

        # progress over the whole chain: every step gets an equal share,
        # within a step the tools report progress lines (arcpak.config.report_progress)
        self.map_progress = QProgressBar()
        self.map_progress.setRange(0, 1000)
        self.map_progress.setValue(0)
        self.map_progress.setFormat("%p %")
        self.map_progress.setVisible(False)
        self.map_timer = QTimer(self)
        self.map_timer.setInterval(1000)
        self.map_timer.timeout.connect(self.update_step_label)
        self._lauf = None                       # (start, step no., count, title, share)

        right = QVBoxLayout()
        right.addWidget(self.map_info)
        right.addWidget(box)
        right.addLayout(btns)
        right.addWidget(self.map_progress)
        right.addStretch(1)

        self.map_log = QPlainTextEdit()
        self.map_log.setReadOnly(True)
        self.map_log.setFont(QFont("Monospace", 9))
        self.map_log.setMaximumBlockCount(6000)
        lay = QVBoxLayout(w)
        row = QHBoxLayout()
        row.addLayout(left)
        row.addLayout(right, 1)
        lay.addLayout(row)
        lay.addWidget(self.map_log, 1)
        return w

    def _count_maps(self):
        if not getattr(self, "_map_counts", None):
            counts = {}
            for c in self.containers:
                for path, ud in c.entries:
                    m = re.search(r"Maps/([^/]+)/", path)
                    if m:
                        counts[m.group(1)] = counts.get(m.group(1), 0) + 1
            self._map_counts = counts
        return self._map_counts

    def load_map_names(self):
        """Map display names from the game's string tables (or .locres)."""
        try:
            self.map_names = map_names(self.containers, self._count_maps(), set_language(self.config.language),
                                       self.config.pak_dir, self.config.aes_keys)
        except Exception as e:
            self.map_names = {}
            print(f"[warn] map names: {e}")

    def build_map_list(self):
        counts = self._count_maps()
        flt = self.map_search.text().strip().lower() if hasattr(self, "map_search") else ""
        cur = self.selected_map()
        self.map_list.blockSignals(True)
        self.map_list.clear()
        # named game maps first (alphabetically), then the remaining folders by size
        order = sorted(counts, key=lambda k: (k not in self.map_names, self.map_names.get(k, "").lower(), -counts[k]))
        for name in order:
            title = self.map_names.get(name)
            if flt and flt not in name.lower() and flt not in (title or "").lower():
                continue
            files = tr("{n} Dateien", n=counts[name])
            it = QListWidgetItem(f"{title}  —  {name}  ({files})" if title else f"{name}  ({files})")
            if title:
                f = it.font(); f.setBold(True); it.setFont(f)
            it.setData(Qt.ItemDataRole.UserRole, name)
            self.map_list.addItem(it)
            if name == cur:
                self.map_list.setCurrentRow(self.map_list.count() - 1)
        self.map_list.blockSignals(False)
        if self.map_list.count() and self.map_list.currentRow() < 0:
            self.map_list.setCurrentRow(0)
        self.map_status()

    def selected_map(self):
        it = self.map_list.currentItem() if hasattr(self, "map_list") else None
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def out_glb(self, name):
        return HERE / "out/map" / (f"{name}_vereinfacht.glb" if self.opt_simplify.isChecked() else f"{name}.glb")

    def map_status(self):
        name = self.selected_map()
        if not name:
            self.map_info.setText("—"); return
        d = HERE / "out/map"
        rows = [(tr("Platzierungen"), d / f"{name}_placements.jsonl"), (tr("Spielbereich"), d / f"{name}_playarea.json"),
                ("GLB", d / f"{name}.glb"), (tr("GLB vereinfacht"), d / f"{name}_vereinfacht.glb")]
        title = self.map_names.get(name)
        lines = [f"<b>{title}</b> &nbsp;<code>{name}</code>" if title else f"<b>{name}</b>"]
        for t, p in rows:
            lines.append(f"{t}: " + (f"{p.stat().st_size / 1e6:,.1f} MB" if p.exists() else f"<i>{tr('fehlt')}</i>"))
        lines.append(tr("Ergebnis dieses Exports:") + f" <code>{self.out_glb(name).relative_to(HERE)}</code>")
        self.map_info.setText("<br>".join(lines))

    def export_steps(self, name):
        py = sys.executable
        steps = []
        jsonl = HERE / "out/map" / f"{name}_placements.jsonl"
        if self.opt_p2.isChecked() or not jsonl.exists():
            steps.append((tr("Platzierungen lesen (P2)"), [py, "tools/mapexport.py", "placements", f"Maps/{name}/"]))
        need_area = self.opt_crop.isChecked() or self.opt_area.isChecked() or self.opt_volumes.isChecked()
        if need_area:
            # no BlockingVolumes/PlayerStart (menu, lobby levels ...): skip the step,
            # the GLB is then built without cropping and without play-area markers
            steps.append((tr("Spielbereich bestimmen (P2b)"), [py, "tools/mapexport.py", "playarea", f"Maps/{name}/"],
                          {"skip_exit": 3, "skip_flags": {"--crop", "--spielbereich", "--volumes"},
                           "skip_text": tr("Kein Spielbereich gefunden — Schritt übersprungen, GLB wird ohne Zuschnitt gebaut")}))
        p3 = [py, "tools/mapexport.py", "glb", name]
        for opt, flag in ((self.opt_crop, "--crop"), (self.opt_leaves, "--ohne-blaetter"),
                          (self.opt_effects, "--ohne-effekte"), (self.opt_helpers, "--ohne-hilfsgeo"),
                          (self.opt_decals, "--ohne-decals"),
                          (self.opt_area, "--spielbereich"), (self.opt_volumes, "--volumes")):
            if opt.isChecked():
                p3.append(flag)
        if not self.opt_terrain.isChecked():
            p3.append("--ohne-terrain")
        steps.append((tr("GLB bauen (P3)"), p3))
        glb = HERE / "out/map" / f"{name}.glb"
        if self.opt_simplify.isChecked():
            steps.append((tr("Vereinfachen auf {pct} (Blender headless)", pct=f"{self.opt_ratio.value():.0%}"),
                          [py, "tools/blender_glb.py", "simplify",
                           str(glb), str(self.out_glb(name)), f"{self.opt_ratio.value():.3f}"]))
        if self.opt_blender.isChecked():
            steps.append((tr("Blender öffnen"), [find_blender(), "--python", "tools/blender_glb.py", "--", "open",
                                             str(self.out_glb(name))]))
        return steps

    def run_export(self):
        name = self.selected_map()
        if not name:
            return
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, tr("Karten"), tr("Es läuft schon ein Export — Log beobachten."))
            return
        steps = self.export_steps(name)
        self.map_log.appendPlainText(f"##### Export {name}: " + " → ".join(s[0] for s in steps))
        self.btn_export.setEnabled(False); self.btn_open.setEnabled(False); self.btn_cancel.setEnabled(True)
        self.worker = ChainWorker(self, steps)
        self.worker.line.connect(self.map_log.appendPlainText)
        self._lauf = [time.monotonic(), 0, len(steps), "", 0.0]
        self.map_progress.setValue(0)
        self.map_progress.setVisible(True)
        self.map_timer.start()

        def schritt(i, n, t):
            self._lauf[1:] = [i, n, t, 0.0]
            self.update_progress()

        def anteil(f):
            if self._lauf:
                self._lauf[4] = f
                self.update_progress()

        def fin(rc, dauer):
            self.map_timer.stop()
            self._lauf = None
            self.btn_export.setEnabled(True); self.btn_open.setEnabled(True); self.btn_cancel.setEnabled(False)
            zeit = dauer_text(dauer)
            if rc == 0:
                self.map_progress.setValue(1000)
                self.map_step.setText(tr("fertig in {zeit}", zeit=zeit))
                self.map_log.appendPlainText(f"##### Export {name}: " + tr("fertig → {path}", path=self.out_glb(name)))
            else:
                self.map_step.setText(tr("abgebrochen nach {zeit} (exit {rc})", zeit=zeit, rc=rc))
                self.map_log.appendPlainText(f"##### Export {name}: " + tr("abgebrochen nach {zeit} (exit {rc})",
                                                                            zeit=zeit, rc=rc))
            self.map_log.appendPlainText("##### " + tr("Gesamtzeit: {zeit}", zeit=zeit))
            self.map_status()

        self.worker.step.connect(schritt)
        self.worker.progress.connect(anteil)
        self.worker.done.connect(fin)
        self.worker.start()

    def update_progress(self):
        if not self._lauf:
            return
        _, i, n, _, f = self._lauf
        self.map_progress.setValue(int(1000 * (max(i - 1, 0) + f) / max(n, 1)))
        self.update_step_label()

    def update_step_label(self):
        if not self._lauf:
            return
        t0, i, n, title, _ = self._lauf
        self.map_step.setText(tr("Schritt {i}/{n}: {title}", i=i, n=n, title=title)
                              + f"  —  {dauer_text(time.monotonic() - t0)}")

    def open_blender(self):
        name = self.selected_map()
        if not name:
            return
        g = self.out_glb(name)
        if not g.exists():
            g = HERE / "out/map" / f"{name}.glb"
        if not g.exists():
            QMessageBox.information(self, tr("Karten"), tr("Noch kein GLB — erst exportieren."))
            return
        subprocess.Popen([find_blender(), "--python", "tools/blender_glb.py", "--", "open", str(g)], cwd=str(HERE),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **detached_kwargs())
        self.map_log.appendPlainText(tr("Blender gestartet: {path}", path=g))

    # ---------- Inventory tab ----------
    def make_inventory_tab(self):
        w = QWidget()
        self.inv_tree = QTreeWidget()
        self.inv_tree.setColumnCount(3)
        self.inv_tree.setHeaderLabels([tr("Ordner"), tr("Dateien"), tr("Bytes")])
        self.inv_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        top = QHBoxLayout()
        rb = QPushButton(tr("Bestand neu zählen")); rb.clicked.connect(self.build_inventory); top.addWidget(rb)
        lay = QVBoxLayout(w)
        lay.addLayout(top)
        lay.addWidget(self.inv_tree, 1)
        self.build_inventory()
        return w

    def build_inventory(self):
        self.inv_tree.clear()
        for d in ("extracted", "extracted/textures_bulk", "extracted/inline", "out/map",
                  "out/arrays", "out", "cache"):
            p = HERE / d
            if not p.is_dir():
                continue
            n = total = 0
            for root, _, files in os.walk(p):
                for f in files:
                    try:
                        total += os.stat(os.path.join(root, f)).st_size
                        n += 1
                    except OSError:
                        pass
            QTreeWidgetItem(self.inv_tree, [d, f"{n:,}", f"{total:,}"])
        self.inv_tree.expandAll()

    # ---------- settings ----------
    def open_settings(self):
        dlg = SettingsDialog(self, self.config)
        if dlg.exec():
            self.config.save()
            if dlg.lang_changed:
                self.restart_ui()
            else:
                self.reload()

    def change_language(self, idx):
        code = self.lang_box.itemData(idx)
        if code == set_language(self.config.language):
            return
        self.config.language = code
        self.config.save()
        self.restart_ui()

    def restart_ui(self):
        """Rebuild the window in the new language (texts are translated while building)."""
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, tr("Karten"), tr("Es läuft schon ein Export — Log beobachten."))
            return
        state = {"containers": self.containers, "models": self.models_tab.state(),
                 "audio": self.audio_tab.state(), "textures": self.textures_tab.state()}
        self.models_tab.release()
        self.audio_tab.release()
        self.textures_tab.release()
        win = MainWindow(state)
        win.setGeometry(self.geometry())
        win.show()
        QApplication.instance().main_window = win
        self.close()


class SettingsDialog(QDialog):
    def __init__(self, parent, cfg):
        super().__init__(parent)
        self.setWindowTitle(tr("Einstellungen"))
        self.resize(640, 420)
        self.cfg = cfg
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.pak = QLineEdit(cfg.pak_dir)
        self.out = QLineEdit(cfg.output_dir)
        self.lang = QComboBox()
        for code, name in LANGUAGES.items():
            self.lang.addItem(name, code)
        self.lang.setCurrentIndex(list(LANGUAGES).index(set_language(cfg.language)))
        self.lang_changed = False
        form.addRow(tr("Paks-Ordner"), self.pak)
        form.addRow(tr("Ausgabeordner"), self.out)
        form.addRow(tr("Sprache"), self.lang)
        lay.addLayout(form)
        lay.addWidget(QLabel(tr("AES-Keys (ohne GUID = allgemein; nach Spiel-Patches aktualisieren):")))
        self.keys = QTableWidget(len(cfg.aes_keys), 2)
        self.keys.setHorizontalHeaderLabels([tr("Name"), tr("Key (hex, ohne 0x)")])
        self.keys.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for r, k in enumerate(cfg.aes_keys):
            self.keys.setItem(r, 0, QTableWidgetItem(k.get("name", "")))
            self.keys.setItem(r, 1, QTableWidgetItem(k.get("key", "")))
        lay.addWidget(self.keys)
        btns = QHBoxLayout()
        add = QPushButton(tr("Key hinzufügen")); add.clicked.connect(lambda: (self.keys.insertRow(self.keys.rowCount()), None))
        rem = QPushButton(tr("Letzten entfernen")); rem.clicked.connect(lambda: self.keys.removeRow(max(0, self.keys.rowCount() - 1)))
        save = QPushButton(tr("Speichern & neu einlesen")); save.clicked.connect(self.accept)
        btns.addWidget(add); btns.addWidget(rem); btns.addStretch(); btns.addWidget(save)
        lay.addLayout(btns)

    def accept(self):
        self.cfg.pak_dir = self.pak.text()
        self.cfg.output_dir = self.out.text()
        code = self.lang.currentData()
        self.lang_changed = code != set_language(self.cfg.language)
        self.cfg.language = code
        self.cfg.aes_keys = [{
            "name": (self.keys.item(r, 0).text() if self.keys.item(r, 0) else ""),
            "guid": "00000000000000000000000000000000",
            "key": (self.keys.item(r, 1).text() if self.keys.item(r, 1) else "").replace("0x", "").strip(),
        } for r in range(self.keys.rowCount())]
        super().accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = MainWindow()
    app.main_window = win
    win.show()
    if "--selftest" in sys.argv:          # offscreen smoke test: mount + screenshot
        if len(sys.argv) > 2:
            win.search.setText(sys.argv[2])
        app.processEvents()
        shot = win.grab()
        out = Path(__file__).parent / "tmp_arcgui.png"
        shot.save(str(out))
        print(f"selftest OK — {len(win.containers)} containers mounted, screenshot → {out}")
        sys.exit(0)
    sys.exit(app.exec())
