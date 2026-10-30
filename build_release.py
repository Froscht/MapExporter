#!/usr/bin/env python3
"""Build the self-contained release/MapExporter.exe with Nuitka.

Nuitka compiles the whole source tree to native C code, so the shipped binary holds no .pyc
bytecode to extract and decompile — a much higher bar than a PyInstaller bundle. One onefile .exe
plays every role (GUI, CLI, Blender helper) via tools/mapexporter_exe.py.

Run from the repo root with the build venv (the global Python carries an obsolete `typing`
backport that trips some build tools):

    build/venv/Scripts/python build_release.py          # Windows
    build/venv/bin/python     build_release.py          # Linux

Writable data (out/, cache/, settings.json) lives next to the .exe (arcpak.config.APP_DIR);
bundled read-only files resolve under arcpak.config.RES_DIR, keeping the source layout.
"""
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # Windows consoles default to cp1252
except AttributeError:
    pass

ROOT = Path(__file__).resolve().parent
WIN = sys.platform == "win32"
KERNEL = f"arcpak/theia_kern.{'dll' if WIN else 'so'}"
# --slim: drop the in-app 3D model preview (pyqtgraph + PyOpenGL + Qt OpenGL) to get the onefile
# under 30 MB. The Models tab still lists and exports models (models_tab falls back on HAVE_GL=False).
SLIM = "--slim" in sys.argv

args = [
    sys.executable, "-m", "nuitka",
    "--onefile",
    "--enable-plugin=pyqt6",
    "--assume-yes-for-downloads",
    # GUI subsystem, but a CLI call from a terminal still attaches to that parent console:
    "--windows-console-mode=attach",
    "--company-name=MapExporter",
    "--product-name=MapExporter",
    "--product-version=1.0.0",
    "--file-version=1.0.0",
    # arcpak must be whole: mapexport.py pulls step modules dynamically (importlib on STEPS).
    "--include-package=arcpak",
    # pyqtgraph/OpenGL are NOT force-included — letting Nuitka follow only the imports that
    # pyqtgraph.opengl actually uses keeps the compiled module out of the tens of MB range.
    # top-level tools/ modules the entry point dispatches to (tools/ is on the compile search path)
    *(f"--include-module={m}" for m in
      ("arcgui", "mapexport", "blender_glb", "models_tab", "audio_tab", "textures_tab", "i18n")),
    # trim: Qt UI translations and Pillow codecs we never use (we only save/load PNG in-memory)
    "--noinclude-qt-translations",
    "--nofollow-import-to=PIL.AvifImagePlugin,PIL.WebPImagePlugin,PIL.TiffImagePlugin,PIL.ImageQt",
    # no network use: drop Python's OpenSSL (libcrypto/libssl ~6 MB). hashlib.sha1 (the only hash we
    # call) falls back to the built-in _sha1; AES comes from pycryptodome's own code, not OpenSSL.
    "--nofollow-import-to=ssl,_ssl,_hashlib",
    # strip docstrings from the compiled module — numpy/pyqtgraph/PIL ship huge ones. Our CLI help
    # lives in USAGE/HELP string constants (not docstrings), so it survives this.
    "--python-flag=no_docstrings",
    # drop Qt bits we never use: QtNetwork + its TLS backends (no networking), QtTest, and every
    # imageformats plugin (we only ever decode/encode PNG, which is built into Qt6Gui — no plugin).
    *(f"--noinclude-dlls={p}" for p in
      ("*qt6network*", "*qt6test*", "*qopensslbackend*", "*qschannelbackend*", "*qcertonlybackend*",
       "*imageformats*")),
    # --slim only: no 3D preview -> drop pyqtgraph, PyOpenGL and the Qt OpenGL libs entirely
    *(["--nofollow-import-to=pyqtgraph,OpenGL,PyQt6.QtOpenGL,PyQt6.QtOpenGLWidgets",
       "--noinclude-dlls=*qt6opengl*"] if SLIM else []),
    # bundled read-only data: kept at the SAME relative paths the source uses (see RES_DIR)
    "--include-data-files=usmap/SDK_Mappings.usmap=usmap/SDK_Mappings.usmap",
    "--include-data-dir=tools/lang=tools/lang",
    "--include-data-files=tools/blender_glb.py=tools/blender_glb.py",
    f"--include-data-files={KERNEL}={KERNEL}",
    "--output-dir=build/nuitka",
    "--output-filename=" + ("MapExporter.exe" if WIN else "MapExporter"),
    "tools/mapexporter_exe.py",
]

print("$ " + " ".join(args), flush=True)
rc = subprocess.run(args, cwd=str(ROOT)).returncode       # cwd=root so `import arcpak` resolves
if rc:
    sys.exit(rc)

built = ROOT / "build" / "nuitka" / ("MapExporter.exe" if WIN else "MapExporter")
dest_dir = ROOT / "release"
dest_dir.mkdir(exist_ok=True)
suffix = "_slim" if SLIM else ""
dest = dest_dir / (built.stem + suffix + built.suffix)       # release/MapExporter_slim.exe for --slim
dest.write_bytes(built.read_bytes())
if not WIN:
    dest.chmod(0o755)
print(f"\nfertig → {dest}  ({dest.stat().st_size / 1e6:.1f} MB)", flush=True)
