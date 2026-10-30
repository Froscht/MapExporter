"""Entry point of the self-contained MapExporter.exe (PyInstaller, see MapExporter.spec).

One binary plays every role the source tree splits into scripts:

    MapExporter.exe                         GUI (tools/arcgui.py)
    MapExporter.exe --selftest [term]       GUI smoke test
    MapExporter.exe <step> …                CLI, same as `python tools/mapexport.py <step> …`
    MapExporter.exe tools/mapexport.py …    what the GUI spawns (it runs sys.executable + script)
    MapExporter.exe tools/blender_glb.py …  Blender helper (simplify/open)

The GUI starts each export step as `[sys.executable, "tools/mapexport.py", …]`; frozen,
sys.executable is this .exe, so the script argument is mapped back to its module here.
"""
import os
import sys
from pathlib import Path

CLI_STEPS = {"placements", "playarea", "glb", "audio", "textures", "modelle", "all", "karte"}


def _attach_console():
    """The .exe is a windowed (GUI-subsystem) program, so a CLI call from cmd/PowerShell has no
    stdout. Attach to the parent console then; pipes from the GUI are inherited and kept."""
    if sys.platform != "win32" or sys.stdout is not None:
        return
    import ctypes
    if ctypes.windll.kernel32.AttachConsole(-1):          # ATTACH_PARENT_PROCESS
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        sys.stderr = sys.stdout
    else:                                                  # no console at all: drop output
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _glcheck():
    """Render a triangle through pyqtgraph.opengl + PyOpenGL and report. Proves the compiled build
    bundles the whole GL path (not just the import), which the 3D model preview depends on. Needs a
    real GL context, so it briefly shows a tiny window. Undocumented smoke test, harmless to ship."""
    from PyQt6.QtWidgets import QApplication
    import numpy as np
    import pyqtgraph.opengl as gl
    app = QApplication(sys.argv)
    view = gl.GLViewWidget()
    view.resize(320, 240)
    view.show()
    md = gl.MeshData(vertexes=np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype="f4"),
                     faces=np.array([[0, 1, 2]]))
    view.addItem(gl.GLMeshItem(meshdata=md, shader="shaded"))
    for _ in range(12):
        app.processEvents()
    img = view.grab()
    print(f"GLCHECK {'ok' if not img.isNull() else 'null'} {img.width()}x{img.height()}", flush=True)


def main():
    args = sys.argv[1:]
    if args and args[0] == "__glcheck":
        _glcheck()
        return
    role = "arcgui"
    if args and Path(args[0]).stem in ("mapexport", "blender_glb", "arcgui"):
        role, args = Path(args[0]).stem, args[1:]
    elif args and args[0] in CLI_STEPS:
        role = "mapexport"
    sys.argv = [sys.argv[0], *args]

    if role == "arcgui":
        import arcgui
        arcgui.main()
        return
    _attach_console()
    if role == "mapexport":
        import mapexport
        mapexport.main()
    else:
        import blender_glb
        blender_glb.main()


if __name__ == "__main__":
    main()
