"""GUI tab "Modelle": browse weapons, ARC enemies, gear and character outfits, preview them in 3D
and export them as GLB. Game data (arcpak.game / arcpak.models) is loaded in a background thread
the first time the tab is opened."""
import os
import subprocess
import sys
from pathlib import Path

import time

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QSlider,
                             QListWidgetItem, QMessageBox, QPushButton, QSplitter, QVBoxLayout, QWidget)

from arcpak.config import APP_DIR
from i18n import tr

HERE = APP_DIR                      # writable root (repo, or the folder of MapExporter.exe)

try:                                    # optional: 3D preview
    import numpy as np
    import pyqtgraph.opengl as gl
    from pyqtgraph import Vector
    HAVE_GL = True
except Exception:                       # pyqtgraph / PyOpenGL missing
    HAVE_GL = False

CATEGORY_LABELS = {"weapons": "Waffen", "bots": "Bots", "gear": "Ausrüstung", "characters": "Charaktere"}
PALETTE = [(0.80, 0.80, 0.82), (0.86, 0.62, 0.42), (0.47, 0.64, 0.82), (0.62, 0.78, 0.50),
           (0.85, 0.75, 0.45), (0.72, 0.55, 0.78), (0.55, 0.75, 0.75), (0.85, 0.55, 0.55)]


class CatalogueWorker(QThread):
    done = pyqtSignal(object, object, str)      # catalogue, faces, error

    def run(self):
        try:
            from arcpak import models
            self.done.emit(models.catalogue(), models.faces(), "")
        except Exception as e:
            self.done.emit(None, None, f"{type(e).__name__}: {e}")


class ModelWorker(QThread):
    done = pyqtSignal(object, object, object, str)      # item, [MeshData], [Clip], error

    def __init__(self, parent, item, body, face, textures, anims):
        super().__init__(parent)
        self.item, self.body, self.face, self.textures, self.anims = item, body, face, textures, anims

    def run(self):
        try:
            from arcpak import models
            meshes, errors = models.build(self.item, body=self.body, face=self.face)
            if self.textures:
                errors += models.apply_textures(meshes)
            clips = []
            if self.anims:
                clips, warn = models.load_clips(self.item, meshes)
                errors += warn
            self.done.emit(self.item, meshes, clips, "\n".join(errors))
        except Exception as e:
            self.done.emit(self.item, [], [], f"{type(e).__name__}: {e}")


def reset_gl_cache():
    """pyqtgraph compiles its shaders once per process and keeps the program ids globally. A new
    GL view (the window is rebuilt after a language change) has a new context in which these ids
    are invalid ("Error while drawing item ..."), so force a recompile."""
    try:
        from pyqtgraph.opengl import shaders
        for prog in shaders.ShaderProgram.names.values():
            prog.prog = None
            for sh in getattr(prog, "shaders", []):
                sh.compiled = None
        # some items (e.g. GLLinePlotItem, used by the grid) cache their program on the class
        from pyqtgraph.opengl.GLGraphicsItem import GLGraphicsItem
        todo = [GLGraphicsItem]
        while todo:
            cls = todo.pop()
            todo.extend(cls.__subclasses__())
            if "_shaderProgram" in cls.__dict__:
                cls._shaderProgram = None
    except Exception:
        pass


def mesh_data(V, F, colors=None):
    """pyqtgraph MeshData with precomputed smooth normals. pyqtgraph's own vertexNormals() loops
    over every vertex in Python (~0.3 s for 80 000 vertices), too slow for animation."""
    from pyqtgraph.opengl import MeshData
    v0, v1, v2 = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    fn = np.cross(v1 - v0, v2 - v0)
    N = np.zeros_like(V)
    for k in range(3):
        N[:, k] = np.bincount(F.ravel(), weights=np.repeat(fn[:, k], 3), minlength=len(V))
    L = np.linalg.norm(N, axis=1, keepdims=True)
    N /= np.where(L > 0, L, 1.0)
    md = MeshData(vertexes=V, faces=F, vertexColors=colors)
    md._vertexNormals = N.astype(np.float32)
    return md


def vertex_colors(m, fallback):
    """Per-vertex RGBA from the base-color textures (sampled at the UVs) and flat slot colors."""
    C = np.empty((len(m.pos), 4), np.float32)
    C[:] = (*fallback, 1.0)
    uv = np.asarray(m.uv, np.float32) if m.uv is not None else None
    for mat, _first, _ntri, v0, v1 in m.sections:
        if mat in m.textures and uv is not None:
            img = np.asarray(m.textures[mat].convert("RGB"), np.float32) / 255.0
            h, w = img.shape[:2]
            u = uv[v0:v1 + 1]
            x = (np.mod(u[:, 0], 1.0) * (w - 1)).astype(np.int32)
            y = (np.mod(u[:, 1], 1.0) * (h - 1)).astype(np.int32)
            C[v0:v1 + 1, :3] = img[y, x]
        elif mat in m.colors:
            C[v0:v1 + 1, :3] = m.colors[mat]
    return C


class ModelsTab(QWidget):
    def __init__(self, parent=None, state=None):
        super().__init__(parent)
        self.catalogue, self.faces_list = None, []
        self.meshes, self.current, self.clips = [], None, []
        self.loader = self.worker = None
        self.gl_items = []

        self.category = QComboBox()
        for key, label in CATEGORY_LABELS.items():
            self.category.addItem(tr(label), key)
        self.category.currentIndexChanged.connect(self.fill_list)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText(tr("Modell filtern…"))
        self.filter.textChanged.connect(self.fill_list)
        self.items = QListWidget()
        self.items.currentItemChanged.connect(lambda *_: self.load_selected())
        left = QVBoxLayout()
        left.addWidget(self.category)
        left.addWidget(self.filter)
        left.addWidget(self.items, 1)
        lw = QWidget(); lw.setLayout(left)

        # character options
        self.opt_body = QCheckBox(tr("Grundkörper"))
        self.opt_body.setChecked(True)
        self.opt_body.toggled.connect(lambda *_: self.load_selected())
        self.face = QComboBox()
        self.face.currentIndexChanged.connect(lambda *_: self.load_selected())
        self.opt_textures = QCheckBox(tr("Texturen"))
        self.opt_textures.setChecked(True)
        self.opt_textures.setToolTip(tr("Farbtexturen laden: im GLB eingebettet, in der Vorschau als Vertexfarben"))
        self.opt_textures.toggled.connect(lambda *_: self.load_selected())
        self.opt_anims = QCheckBox(tr("Animationen"))
        self.opt_anims.setChecked(True)
        self.opt_anims.setToolTip(tr("Animationen des Modells mit Skelett ins GLB exportieren (in Blender abspielbar)"))
        self.opt_anims.toggled.connect(lambda *_: self.load_selected())
        self.char_opts = QWidget()
        co = QHBoxLayout(self.char_opts); co.setContentsMargins(0, 0, 0, 0)
        co.addWidget(self.opt_body); co.addWidget(QLabel(tr("Gesicht:"))); co.addWidget(self.face); co.addStretch(1)
        self.char_opts.setVisible(False)

        if HAVE_GL:
            reset_gl_cache()
            self.view = gl.GLViewWidget()
            self.view.setBackgroundColor((40, 42, 46))
            self.view.setMinimumHeight(360)
            grid = gl.GLGridItem()
            grid.setSize(400, 400); grid.setSpacing(25, 25)
            self.view.addItem(grid)
            self.grid = grid
        else:
            self.view = QLabel(tr("3D-Vorschau braucht pyqtgraph und PyOpenGL (pip install -r requirements.txt)."))
            self.view.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.view.setWordWrap(True)
        self.info = QLabel("—")
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        btns = QHBoxLayout()
        self.btn_export = QPushButton(tr("Als GLB exportieren"))
        self.btn_export.setStyleSheet("font-weight: bold; padding: 6px 18px;")
        self.btn_export.clicked.connect(self.export)
        self.btn_blender = QPushButton(tr("In Blender öffnen"))
        self.btn_blender.clicked.connect(self.open_blender)
        self.btn_folder = QPushButton(tr("Ordner öffnen"))
        self.btn_folder.clicked.connect(self.open_folder)
        for b in (self.btn_export, self.btn_blender, self.btn_folder):
            btns.addWidget(b)
        btns.addStretch(1)
        for b in (self.btn_export, self.btn_blender):
            b.setEnabled(False)

        top = QHBoxLayout()
        top.addWidget(self.opt_textures)
        top.addWidget(self.opt_anims)
        top.addWidget(self.char_opts, 1)
        right = QVBoxLayout()
        right.addLayout(top)
        right.addWidget(self.view, 1)

        # animation preview: clip, play/pause, time slider
        self.anim_bar = QWidget()
        ab = QHBoxLayout(self.anim_bar); ab.setContentsMargins(0, 0, 0, 0)
        self.clip_box = QComboBox()
        self.clip_box.setMinimumWidth(240)
        self.clip_box.currentIndexChanged.connect(self.select_clip)
        self.btn_play = QPushButton(tr("Abspielen"))
        self.btn_play.clicked.connect(self.toggle_play)
        self.time_slider = QSlider(Qt.Orientation.Horizontal)
        self.time_slider.setRange(0, 1000)
        self.time_slider.sliderMoved.connect(self.scrub)
        self.time_label = QLabel("0.00 s")
        self.time_label.setMinimumWidth(90)
        ab.addWidget(QLabel(tr("Animation:"))); ab.addWidget(self.clip_box)
        ab.addWidget(self.btn_play); ab.addWidget(self.time_slider, 1); ab.addWidget(self.time_label)
        self.anim_bar.setVisible(False)
        right.addWidget(self.anim_bar)
        self.poser, self.clip, self.anim_t, self.playing = None, None, 0.0, False
        self._last_tick = 0.0
        self.anim_timer = QTimer(self)
        self.anim_timer.setInterval(33)
        self.anim_timer.timeout.connect(self.tick)
        self.gl_parts = []                      # (GLMeshItem, faces, colors, mesh index)

        right.addWidget(self.info)
        right.addLayout(btns)
        rw = QWidget(); rw.setLayout(right)

        split = QSplitter()
        split.addWidget(lw); split.addWidget(rw)
        split.setStretchFactor(1, 1)
        split.setSizes([320, 900])
        lay = QVBoxLayout(self)
        lay.addWidget(split)
        if state:
            self._catalogue_done(state["catalogue"], state["faces"], "")

    def state(self):
        """Catalogue to hand over to a rebuilt window (None if not loaded yet)."""
        return {"catalogue": self.catalogue, "faces": self.faces_list} if self.catalogue else None

    def release(self):
        """Drop the GL items before the window goes away."""
        self.stop_anim()
        if self.worker and self.worker.isRunning():
            self.worker.done.disconnect()
        if HAVE_GL:
            for gi in self.gl_items:
                self.view.removeItem(gi)
            self.gl_items = []

    # ---------- catalogue ----------
    def ensure_loaded(self):
        """Load the catalogue in the background (first time the tab is shown)."""
        if self.catalogue is not None or self.loader is not None:
            return
        self.items.clear()
        self.items.addItem(tr("Lade Spieldaten…"))
        self.loader = CatalogueWorker(self)
        self.loader.done.connect(self._catalogue_done)
        self.loader.start()

    def _catalogue_done(self, cat, faces, err):
        if err:
            self.items.clear()
            self.items.addItem(tr("Fehler: {err}", err=err))
            return
        self.catalogue, self.faces_list = cat, faces
        self.face.blockSignals(True)
        self.face.addItem(tr("kein Kopf"), None)
        for f in faces:
            self.face.addItem(f.replace("_Pio", "").replace("_", " "), f)
        self.face.setCurrentIndex(1 if faces else 0)
        self.face.blockSignals(False)
        self.fill_list()

    def fill_list(self):
        if not self.catalogue:
            return
        key = self.category.currentData()
        self.char_opts.setVisible(key == "characters")
        self.opt_anims.setVisible(key != "characters")
        flt = self.filter.text().strip().lower()
        self.items.blockSignals(True)
        self.items.clear()
        for it in self.catalogue.get(key, []):
            label = it.name + (f"  —  {it.folder}" if it.folder and it.folder.lower() not in it.name.lower().replace(" ", "_") else "")
            if flt and flt not in label.lower():
                continue
            li = QListWidgetItem(label)
            li.setData(Qt.ItemDataRole.UserRole, it)
            self.items.addItem(li)
        self.items.blockSignals(False)
        self.info.setText(tr("{n} Modelle", n=self.items.count()))

    # ---------- loading / preview ----------
    def selected_item(self):
        li = self.items.currentItem()
        return li.data(Qt.ItemDataRole.UserRole) if li else None

    def load_selected(self):
        it = self.selected_item()
        if it is None or not hasattr(it, "category"):
            return
        if self.worker and self.worker.isRunning():
            self.worker.done.disconnect()
        self.info.setText(tr("Lade {name}…", name=it.name))
        self.btn_export.setEnabled(False)
        self.worker = ModelWorker(self, it, self.opt_body.isChecked(), self.face.currentData(),
                                  self.opt_textures.isChecked(),
                                  self.opt_anims.isChecked() and it.category != "characters")
        self.worker.done.connect(self._model_done)
        self.worker.start()

    def _model_done(self, item, meshes, clips, err):
        if item is not self.selected_item():
            return
        self.stop_anim()
        self.current, self.meshes, self.clips = item, meshes, clips
        verts = sum(len(m.pos) for m in meshes)
        tris = sum(m.triangles for m in meshes)
        text = tr("<b>{name}</b> — {n} Meshes, {v} Vertices, {t} Dreiecke",
                  name=item.name, n=len(meshes), v=f"{verts:,}", t=f"{tris:,}")
        if len(meshes) > 1:
            text += "<br><small>" + ", ".join(m.name for m in meshes) + "</small>"
        if clips:
            text += "<br>" + tr("{n} Animationen", n=len(clips)) + ": <small>" + \
                ", ".join(c.name.split("_", 3)[-1] for c in clips) + "</small>"
        if err:
            text += "<br><span style='color:#e88'>" + err.replace("\n", "<br>") + "</span>"
        self.info.setText(text)
        self.btn_export.setEnabled(bool(meshes))
        self.btn_blender.setEnabled(bool(meshes))
        self.show_meshes(meshes)
        self.setup_anim(meshes, clips)

    def show_meshes(self, meshes):
        if not HAVE_GL:
            return
        for gi in self.gl_items:
            self.view.removeItem(gi)
        self.gl_items, self.gl_parts = [], []
        lo = np.array([1e30] * 3); hi = -lo
        for i, m in enumerate(meshes):
            if len(m.pos) == 0 or len(m.idx) == 0:
                continue
            V = np.asarray(m.pos, dtype=np.float32)
            V[:, 1] *= -1                       # Unreal is left-handed: mirror Y for display
            faces = []
            for _mat, first, ntri, _v0, _v1 in m.sections:
                faces.append(np.asarray(m.idx[first:first + 3 * ntri], dtype=np.uint32).reshape(-1, 3))
            F = np.concatenate(faces) if faces else np.zeros((0, 3), np.uint32)
            F = F[:, ::-1]                      # keep the winding after the mirror
            r, g, b = PALETTE[i % len(PALETTE)]
            colors = vertex_colors(m, (r, g, b)) if (m.textures or m.colors) else None
            item = gl.GLMeshItem(meshdata=mesh_data(V, F, colors), smooth=True, shader="shaded",
                                 color=(r, g, b, 1.0), drawEdges=False)
            self.view.addItem(item)
            self.gl_items.append(item)
            self.gl_parts.append((item, F, colors, i))
            lo = np.minimum(lo, V.min(0)); hi = np.maximum(hi, V.max(0))
        if self.gl_items:
            c = (lo + hi) / 2
            size = float(max(hi - lo))
            self.grid.resetTransform()
            self.grid.translate(c[0], c[1], lo[2])
            step = max(1.0, round(size / 10.0))
            self.grid.setSize(size * 2, size * 2); self.grid.setSpacing(step, step)
            self.view.opts["center"] = Vector(float(c[0]), float(c[1]), float(c[2]))
            self.view.setCameraPosition(distance=size * 1.8, elevation=12, azimuth=-90)

    # ---------- animation preview ----------
    def setup_anim(self, meshes, clips):
        self.poser = None
        self.clip_box.blockSignals(True)
        self.clip_box.clear()
        if HAVE_GL and clips:
            try:
                from arcpak import anim
                self.poser = anim.Poser(meshes)
            except Exception:
                self.poser = None
        if self.poser:
            for c in clips:
                self.clip_box.addItem(c.name.split("_", 3)[-1] if c.name.count("_") >= 3 else c.name, c)
        self.clip_box.blockSignals(False)
        self.anim_bar.setVisible(self.poser is not None)
        if self.poser:
            self.select_clip(0)

    def select_clip(self, _i=None):
        self.clip = self.clip_box.currentData()
        self.anim_t = 0.0
        self.apply_pose()

    def toggle_play(self):
        if self.playing:
            self.stop_anim()
        elif self.clip is not None:
            self.playing = True
            self._last_tick = time.monotonic()
            self.btn_play.setText(tr("Pause"))
            self.anim_timer.start()

    def stop_anim(self):
        self.playing = False
        self.anim_timer.stop()
        self.btn_play.setText(tr("Abspielen"))

    def tick(self):
        if not self.clip:
            return
        now = time.monotonic()
        self.anim_t += now - self._last_tick
        self._last_tick = now
        if self.clip.length > 0:
            self.anim_t %= self.clip.length       # loop
        self.apply_pose()

    def scrub(self, v):
        if self.clip:
            self.anim_t = self.clip.length * v / 1000.0
            self.apply_pose()

    def apply_pose(self):
        if not (self.poser and self.clip):
            return
        positions = self.poser.evaluate(self.clip, self.anim_t)
        for item, F, colors, i in self.gl_parts:
            V = positions[i].astype(np.float32)
            V[:, 1] *= -1                          # same mirror as show_meshes
            item.setMeshData(meshdata=mesh_data(V, F, colors))
        L = self.clip.length or 1.0
        self.time_slider.blockSignals(True)
        self.time_slider.setValue(int(1000 * self.anim_t / L))
        self.time_slider.blockSignals(False)
        self.time_label.setText(f"{self.anim_t:.2f} / {self.clip.length:.2f} s")

    # ---------- export ----------
    def export(self):
        if not self.meshes or self.current is None:
            return None
        from arcpak import models
        path = models.out_path(self.current)
        try:
            models.export_glb(self.meshes, path, clips=self.clips if self.opt_anims.isChecked() else None)
        except Exception as e:
            QMessageBox.warning(self, tr("Modelle"), f"{type(e).__name__}: {e}")
            return None
        self.info.setText(self.info.text() + "<br>" + tr("exportiert → {path}", path=os.path.relpath(path, HERE)))
        return path

    def open_blender(self):
        path = self.export()
        if path:
            subprocess.Popen([sys.executable, str(HERE / "tools" / "blender_glb.py"), "open", path], cwd=str(HERE))

    def open_folder(self):
        from arcpak.config import open_folder
        key = self.category.currentData()
        open_folder(HERE / "out" / "models" / key)
