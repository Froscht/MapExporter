"""Settings (arcpak/settings.json), platform helpers and tool progress reporting.

Update the AES keys and the FFmpeg path in the settings when the game patches them or you install
another FFmpeg build, no code changes needed.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


# ---------- platform helpers ----------
WINDOWS = sys.platform == "win32"
PAKS_REL = Path("steamapps/common/Arc Raiders/PioneerGame/Content/Paks")


def _steam_roots() -> list[Path]:
    roots = []
    if WINDOWS:
        try:
            import winreg
            for hive, key, val in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                                   (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath")):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        roots.append(Path(winreg.QueryValueEx(k, val)[0]))
                except OSError:
                    pass
        except ImportError:
            pass
        roots.append(Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam")
    else:
        roots += [Path.home() / ".local/share/Steam", Path.home() / ".steam/steam",
                  Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam"]
    # additional Steam libraries from libraryfolders.vdf
    for r in list(roots):
        vdf = r / "steamapps" / "libraryfolders.vdf"
        try:
            for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="replace")):
                roots.append(Path(m.group(1).replace("\\\\", "\\")))
        except OSError:
            pass
    seen, out = set(), []
    for r in roots:
        k = os.path.normcase(str(r))
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def find_pak_dir() -> str:
    """Locate the ARC Raiders Paks folder; otherwise return the usual default path."""
    roots = _steam_roots()
    for r in roots:
        p = r / PAKS_REL
        if (p / "global.utoc").exists():
            return str(p)
    return str((roots[0] if roots else Path.home()) / PAKS_REL)


def find_blender() -> str:
    """Blender executable: $BLENDER > PATH > default install locations (Windows) > 'blender'."""
    env = os.environ.get("BLENDER")
    if env:
        return env
    found = shutil.which("blender")
    if found:
        return found
    if WINDOWS:
        cands = []
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            cands += glob.glob(os.path.join(base, "Blender Foundation", "Blender*", "blender.exe"))
        for r in _steam_roots():
            cands += glob.glob(str(r / "steamapps/common/Blender/blender.exe"))
        if cands:
            def ver(p):
                m = re.search(r"Blender\s*(\d+)\.(\d+)", p)
                return (int(m.group(1)), int(m.group(2))) if m else (0, 0)
            return max(cands, key=ver)
    return "blender"


def open_folder(path) -> None:
    path = str(path)
    os.makedirs(path, exist_ok=True)
    if WINDOWS:
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def detached_kwargs() -> dict:
    """Popen kwargs that let a GUI program keep running independently."""
    if WINDOWS:
        return {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


# ---------- settings ----------
DEFAULT_CONFIG_PATH = Path(__file__).parent / "settings.json"

DEFAULT_CONFIG = {
    "pak_dir": find_pak_dir(),
    "game": "arcraiders",
    "language": "",
    "output_dir": "extracted",
    "oodle_lib": "",
    "ffmpeg": "",                               # path to an ffmpeg binary/folder that decodes UEBA
    "aes_keys": [
        {
            "name": "Arc Raiders AES key (2026-06)",
            "guid": "00000000000000000000000000000000",
            "key": "047A8AC14396604CE1BAB46366C0A7FDBE40F66264D4625E2E6D11FF17272D7F",
        }
    ],
    "note": "utoc/Theia hardcoding lives in arcpak (crypto.py, iostore.py); these AES keys are used for legacy pak indexes.",
}


@dataclass
class Config:
    pak_dir: str = DEFAULT_CONFIG["pak_dir"]
    game: str = "arcraiders"
    language: str = ""          # GUI language ("" = system language)
    output_dir: str = "extracted"
    oodle_lib: str = ""
    ffmpeg: str = ""        # FFmpeg build for BinkA playback ("" = search $ARC_FFMPEG/PATH/etc.)
    aes_keys: list = field(default_factory=lambda: [dict(k) for k in DEFAULT_CONFIG["aes_keys"]])

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG_PATH) -> "Config":
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(**{k: data.get(k, getattr(cls(), k, None)) for k in
                          ("pak_dir", "game", "language", "output_dir", "oodle_lib", "ffmpeg",
                           "aes_keys")})
        return cls()

    def save(self, path: Path = DEFAULT_CONFIG_PATH):
        path.write_text(json.dumps({
            "pak_dir": self.pak_dir, "game": self.game, "language": self.language,
            "output_dir": self.output_dir, "oodle_lib": self.oodle_lib, "ffmpeg": self.ffmpeg,
            "aes_keys": self.aes_keys, "note": DEFAULT_CONFIG["note"],
        }, indent=2), encoding="utf-8")


# ---------- progress reporting ----------
PROGRESS_PREFIX = "@@PROGRESS"
_last_report = 0.0


def report_progress(done: int, total: int) -> None:
    """Print a progress line for the GUI ("@@PROGRESS <done> <total>"), at most ~5 per second.
    The GUI turns it into the progress bar and hides it from the log."""
    global _last_report
    now = time.monotonic()
    if done < total and now - _last_report < 0.2:
        return
    _last_report = now
    print(f"{PROGRESS_PREFIX} {done} {max(total, 1)}", flush=True)


def parse_progress(line: str):
    """(done, total) from an output line, or None."""
    if not line.startswith(PROGRESS_PREFIX):
        return None
    try:
        a, b = line[len(PROGRESS_PREFIX):].split()
        return int(a), int(b)
    except ValueError:
        return None
