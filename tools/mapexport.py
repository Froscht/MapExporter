#!/usr/bin/env python3
"""Map export from the command line (the GUI runs the same steps).

    python tools/mapexport.py placements Maps/<Map>/   [--out=...jsonl]
    python tools/mapexport.py playarea   Maps/<Map>/   [--cell=100] [--seed=x,y] [--strikt] [--hang=44.765|aus]
    python tools/mapexport.py glb        <Map> [placements.jsonl] [--out=...glb] [--crop] [--ohne-blaetter]
                                         [--ohne-effekte] [--ohne-hilfsgeo] [--ohne-decals]
                                         [--spielbereich] [--volumes] [--ohne-terrain]
    python tools/mapexport.py all        [Map ...] [--ratio=0.25] [--ohne-vereinfachen] [--ohne-crop]
                                         [--mit-blaettern] [--mit-effekten] [--mit-hilfsgeo]
                                         [--mit-decals] [--ab=<Map>] [--neu]
    python tools/mapexport.py textures   list|info|export …   (own help: call `textures` bare)
    python tools/mapexport.py audio      list|info|export …   (own help: call `audio` bare)

Steps: placements (all static-mesh placements of a map -> out/map/<Map>_placements.jsonl),
playarea (walkable area -> out/map/<Map>_playarea.json; exit code 3 = the map has none),
glb (GLB of the map -> out/map/<Map>.glb). `all` runs every map in turn, simplifies the result
with Blender and writes one log per map to out/map/logs/. textures/audio are the browsable
catalogues: PNG -> out/textures/, WAV + UEBA (.binka) -> out/audio/.
"""
import os
import re
import runpy
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STEPS = {"placements": "arcpak.placements", "playarea": "arcpak.playarea", "glb": "arcpak.mapglb",
         "audio": "arcpak.audio", "textures": "arcpak.texture"}
EXIT_NO_PLAYAREA = 3


def map_list():
    from arcpak.config import Config
    from arcpak.iostore import mount_directory
    n = {}
    for c in mount_directory(Config.load().pak_dir):
        for path, _ud in c.entries:
            m = re.search(r"Maps/([^/]+)/", path)
            if m and path.endswith(".umap"):
                n[m.group(1)] = n.get(m.group(1), 0) + 1
    return sorted(n, key=lambda k: n[k])          # small maps first: quick feedback


def export_all(argv):
    """Export every map strictly one after another (memory). A failing step marks the map as
    failed and continues with the next one; no play area -> GLB without cropping."""
    out_dir = os.path.join(HERE, "out", "map")
    logs = os.path.join(out_dir, "logs")
    os.makedirs(logs, exist_ok=True)
    names = [a for a in argv if not a.startswith("--")]
    ratio = float(next((a[8:] for a in argv if a.startswith("--ratio=")), 0.25))
    simplify = "--ohne-vereinfachen" not in argv
    crop_wanted = "--ohne-crop" not in argv
    rebuild = "--neu" in argv
    start = next((a[5:] for a in argv if a.startswith("--ab=")), None)
    filters = [flag for flag, keep in (("--ohne-blaetter", "--mit-blaettern"), ("--ohne-effekte", "--mit-effekten"),
                                       ("--ohne-hilfsgeo", "--mit-hilfsgeo"), ("--ohne-decals", "--mit-decals"))
               if keep not in argv]

    maps = names or map_list()
    if start:
        maps = maps[maps.index(start):]
    print(f"{len(maps)} Maps: {', '.join(maps)}", flush=True)
    py, me = sys.executable, os.path.abspath(__file__)
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")

    def run(log, title, cmd):
        log.write(f"\n=== {title}\n$ {' '.join(cmd)}\n"); log.flush()
        t = time.time()
        r = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, env=env, cwd=HERE)
        log.write(f"--- exit {r.returncode}, {time.time() - t:.1f} s\n"); log.flush()
        return r.returncode

    report = []
    t_total = time.time()
    for name in maps:
        glb = os.path.join(out_dir, f"{name}.glb")
        if not rebuild and os.path.exists(glb):
            report.append((name, "übersprungen (GLB existiert)", "", os.path.getsize(glb), 0.0))
            print(f"[{name}] übersprungen — GLB existiert (--neu zum Neubauen)", flush=True)
            continue
        t0 = time.time()
        status, area = "ok", ""
        with open(os.path.join(logs, f"{name}.log"), "w", encoding="utf-8") as log:
            print(f"[{name}] P2 Platzierungen …", flush=True)
            if run(log, "P2", [py, me, "placements", f"Maps/{name}/"]):
                status = "FEHLER P2"
            crop = False
            if status == "ok" and crop_wanted:
                print(f"[{name}] P2b Spielbereich …", flush=True)
                rc = run(log, "P2b", [py, me, "playarea", f"Maps/{name}/"])
                if rc == 0:
                    import json
                    crop = True
                    with open(os.path.join(out_dir, f"{name}_playarea.json"), encoding="utf-8") as f:
                        pa = json.load(f)
                    area = f"{pa['flaeche_m2']:,.0f} m²" + ("" if pa.get("geschlossen", True) else " (Näherung)")
                elif rc == EXIT_NO_PLAYAREA:
                    area = "keiner → ohne Zuschnitt"
                else:
                    area = f"FEHLER P2b (exit {rc}) → ohne Zuschnitt"
            if status == "ok":
                print(f"[{name}] P3 GLB …", flush=True)
                if run(log, "P3", [py, me, "glb", name] + (["--crop"] if crop else []) + filters):
                    status = "FEHLER P3"
            if status == "ok" and simplify:
                print(f"[{name}] Vereinfachen {ratio:.0%} …", flush=True)
                if run(log, "Vereinfachen", [py, os.path.join(HERE, "tools", "blender_glb.py"), "simplify", glb,
                                             os.path.join(out_dir, f"{name}_vereinfacht.glb"), str(ratio)]):
                    status = "FEHLER Vereinfachen"
        dt = time.time() - t0
        size = os.path.getsize(glb) if os.path.exists(glb) else 0
        report.append((name, status, area, size, dt))
        print(f"[{name}] {status} — {area} — {size / 1e6:.1f} MB — {dt:.0f} s", flush=True)

    print("\nMap                      Status                        Spielbereich                 GLB MB   Zeit s")
    for name, st, area, size, dt in report:
        print(f"{name:24} {st:29} {area:28} {size / 1e6:7.1f} {dt:8.0f}")
    g = int(time.time() - t_total)
    print(f"\nGesamtzeit: {g // 3600}:{g % 3600 // 60:02d}:{g % 60:02d} ({g} s)")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # Windows consoles default to cp1252
    except AttributeError:
        pass
    if len(sys.argv) < 2 or sys.argv[1] not in (*STEPS, "all"):
        raise SystemExit(__doc__)
    cmd, rest = sys.argv[1], sys.argv[2:]
    sys.path.insert(0, HERE)
    if cmd == "all":
        export_all(rest)
        return
    sys.argv = [f"mapexport {cmd}", *rest]
    runpy.run_module(STEPS[cmd], run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
