"""Offscreen screenshots of each GUI tab for the README / a forum post.

    QT_QPA_PLATFORM=offscreen python tools/_shots.py

Builds the real MainWindow, mounts the game data, visits every tab, waits for its background
catalogue to load, and grabs it to out/screenshots/. Not shipped; a dev helper like _i18n_check.
"""
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tools"))

from PyQt6.QtWidgets import QApplication
import arcgui

OUT = HERE / "out" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)
SIZE = (1360, 860)


def pump(seconds, until=None):
    """Run the event loop for a while, stopping early once `until()` is true."""
    app = QApplication.instance()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.03)
        if until and until():
            # give the UI a few more frames to paint the freshly filled widgets
            for _ in range(10):
                app.processEvents(); time.sleep(0.03)
            return


def grab(win, name):
    path = OUT / f"{name}.png"
    win.grab().save(str(path))
    print(f"  {name:10} {path.stat().st_size // 1024:4} KB", flush=True)


def main():
    app = QApplication(sys.argv)
    win = arcgui.MainWindow()
    win.resize(*SIZE)
    win.show()
    pump(3)                                   # browser tab is filled by reload() at build time
    tabs = win.tabs

    names = {}
    for i in range(tabs.count()):
        names[tabs.tabText(i).lower()] = i

    order = [("browser", None), ("karten", None), ("modelle", win.models_tab),
             ("audio", win.audio_tab), ("texturen", win.textures_tab), ("bestand", None)]
    label = {"karten": "maps", "modelle": "models", "texturen": "textures", "bestand": "inventory"}

    for tab_name, tab in order:
        idx = names.get(tab_name)
        if idx is None:
            continue
        tabs.setCurrentIndex(idx)
        pump(1)
        if tab is not None:                   # lazy catalogue tabs: wait until the list fills
            pump(40, until=lambda t=tab: getattr(t, "catalogue", None) is not None and t.items.count() > 0)
            if tab_name == "texturen" and tab.items.count():     # show a preview image too
                tab.items.setCurrentRow(0)
                pump(15, until=lambda t=tab: not t.preview.pixmap().isNull())
                pump(2)
            if tab_name == "modelle" and tab.items.count():       # load a weapon into the 3D view
                tab.items.setCurrentRow(0)
                pump(30, until=lambda t=tab: getattr(t, "current", None) is not None and bool(t.meshes))
                pump(3)                                            # let the GL viewport paint
        grab(win, label.get(tab_name, tab_name))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    main()
