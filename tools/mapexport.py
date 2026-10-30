#!/usr/bin/env python3
"""Map export from the command line (the GUI runs the same steps). Usage text is in USAGE below,
not this docstring, so it survives a --python-flag=no_docstrings release build (see build_release.py).

Steps: placements (all static-mesh placements of a map -> out/map/<Map>_placements.jsonl),
playarea (walkable area -> out/map/<Map>_playarea.json; exit code 3 = the map has none),
glb (GLB of the map -> out/map/<Map>.glb). Each step is an importable library function
(arcpak.<step>.run), so this dispatcher calls it in-process instead of re-running a module.
`karte` runs the three steps in ONE process (recommended one-map command); `all` runs every map in
turn via `karte`, simplifies with Blender and logs to out/map/logs/.
"""
import os
import re
import subprocess
import sys
import time

USAGE = """Map export from the command line (the GUI runs the same steps).

    python tools/mapexport.py placements Maps/<Map>/   [--out=...jsonl]
    python tools/mapexport.py playarea   Maps/<Map>/   [--cell=100] [--seed=x,y] [--strikt] [--hang=44.765|aus]
    python tools/mapexport.py glb        <Map> [placements.jsonl] [--out=...glb] [--crop] [--ohne-blaetter]
                                         [--ohne-effekte] [--ohne-hilfsgeo] [--ohne-decals]
                                         [--spielbereich] [--volumes] [--ohne-terrain]
    python tools/mapexport.py karte      <Map> [--ohne-crop] [--ohne-platzierungen]
                                         [--cell=100] [--seed=x,y] [--strikt] [--hang=44.765|aus]
                                         [--ohne-blaetter] [--ohne-effekte] [--ohne-hilfsgeo]
                                         [--ohne-decals] [--spielbereich] [--volumes] [--ohne-terrain]
                                         [--out=...glb]
    python tools/mapexport.py all        [Map ...] [--ratio=0.25] [--ohne-vereinfachen] [--ohne-crop]
                                         [--mit-blaettern] [--mit-effekten] [--mit-hilfsgeo]
                                         [--mit-decals] [--ab=<Map>] [--neu]
    python tools/mapexport.py textures   list|info|export …   (own help: call `textures` bare)
    python tools/mapexport.py audio      list|info|export|play|ffmpeg …   (own help: call `audio` bare)
    python tools/mapexport.py modelle    list|info|export …   (own help: call `modelle` bare)
"""

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STEPS = {"placements": "arcpak.placements", "playarea": "arcpak.playarea", "glb": "arcpak.mapglb",
         "audio": "arcpak.audio", "textures": "arcpak.texture", "modelle": "arcpak.models"}


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
            cmd = [py, me, "karte", name] + ([] if crop_wanted else ["--ohne-crop"]) + filters
            rc = run(log, "karte (P2+P2b+P3)", cmd)
            if rc:
                status = "FEHLER karte"
            if status == "ok" and os.path.exists(os.path.join(out_dir, f"{name}_playarea.json")):
                import json
                with open(os.path.join(out_dir, f"{name}_playarea.json"), encoding="utf-8") as f:
                    pa = json.load(f)
                area = f"{pa['flaeche_m2']:,.0f} m²" + ("" if pa.get("geschlossen", True) else " (Näherung)")
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
    if len(sys.argv) < 2 or sys.argv[1] not in (*STEPS, "all", "karte"):
        raise SystemExit(USAGE)
    cmd, rest = sys.argv[1], sys.argv[2:]
    sys.path.insert(0, HERE)
    if cmd == "all":
        export_all(rest)
        return
    if cmd == "karte":
        run_karte(rest)
        return
    # In-process dispatch: the step modules are importable libraries, so there is no second
    # module instance and no runpy overhead.
    import importlib
    mod = importlib.import_module(STEPS[cmd])
    mod.main(rest)               # output path; a no-play-area map raises SystemExit(3)


def run_karte(argv):
    """placements → playarea → glb in ONE process (see the module docstring)."""
    from arcpak import mapglb, placements, playarea
    args = list(argv)
    positional = [a for a in args if not a.startswith("--")]
    if not positional:
        raise SystemExit("karte braucht einen Map-Namen oder Pfad, z. B. PracticeRangeArray")
    name = positional[0].strip("/").split("/")[-1]
    sub = positional[0] if "/" in positional[0] else f"Maps/{name}/"

    def opt(key, default=None):
        return next((a[len(key) + 1:] for a in args if a.startswith(key + "=")), default)

    t0 = time.time()
    if "--ohne-platzierungen" not in args:
        print(f"[{name}] P2 Platzierungen …", flush=True)
        placements.run(sub, opt("--placements"))
    crop = "--ohne-crop" not in args
    if crop:
        print(f"[{name}] P2b Spielbereich …", flush=True)
        seed = opt("--seed")
        try:
            playarea.run(sub, cell=float(opt("--cell", 100)), strikt="--strikt" in args,
                         hang=opt("--hang", "44.765"),
                         seed=tuple(map(float, seed.split(","))) if seed else None)
        except playarea.NoPlayArea as e:
            print(f"[{name}] kein Spielbereich ({e.grund}) → ohne Zuschnitt", flush=True)
            crop = False
    print(f"[{name}] P3 GLB …", flush=True)
    out = mapglb.run(**mapglb.parse_options(
        [name] + [a for a in args if a.startswith("--out=")]
        + (["--crop"] if crop else [])
        + [a for a in args if a in ("--spielbereich", "--volumes", "--ohne-terrain",
                                    "--ohne-blaetter", "--ohne-effekte",
                                    "--ohne-hilfsgeo", "--ohne-decals")]))
    print(f"[{name}] fertig in {time.time() - t0:.0f} s", flush=True)
    return out


if __name__ == "__main__":
    main()
