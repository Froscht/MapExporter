"""arcpak.placements — world placements of all StaticMesh components of a map.

For each component (class chain contains StaticMeshComponent and the property reader consumed the
export exactly):
    Mesh       StaticMesh from the export itself or from the template chain
               (import → package → PublicExportHash), or local
               (LandscapeMeshProxy → StaticMesh export of the same cell)
    World      FTransform chain along AttachParent (Relative * ParentWorld); missing Relative* fields
               are taken from the template chain. ISM: per-instance FMatrix · component transform
               (attach chain limited to depth 12)
    Checks     (e) every mesh reference points to a StaticMesh export
               (f) components with cooked world bounds (SceneComponent block; FBoxSphereBounds in
                   the order Origin, BoxExtent, SphereRadius): the mesh's RenderData bounds are
                   mapped through the computed world transform (ISM: union over all instances)
                   and compared with the stored bounds.
                   Control: the same computation without the parent chain — for components with a
                   parent the control must FAIL, otherwise the chain has no effect.

    python tools/mapexport.py placements <map sub-path> [--out=out/map/<Name>_placements.jsonl]

Importing this module has no side effects: no container is mounted and no command line is read.
The API is Options + run(); main() is the CLI entry point. Run state lives in the module globals
below and is reset by run(), so one process can export several maps in a row.

Output is JSONL, one line per placement (ISM: one per instance):
    cell, name, class, mesh {path,hash} or {local_cell,export}, mesh_name,
    world (16 values, row-major UE matrix, cm), rel {loc,rot,scale}, ism (instance index or null),
    tiefe (attach-chain depth, 0 = no parent), hidden, socket, garbage (implausible translation),
    spline, materials (OverrideMaterials), bounds (stored), bc/bc_do/bc_de (bounds check),
    bc_ctrl (check without parent chain), spline_params (SplineMeshComponent only, arcpak.mesh.params)
"""
import collections
import json
import os
import struct
import sys
from dataclasses import dataclass

from . import game, iostore, mesh
from . import uetransform as ut
from . import zen
from .config import APP_DIR, report_progress
from .game import chain_names, native_start

HERE = str(APP_DIR)            # writable root (repo, or the folder of MapExporter.exe)

@dataclass
class Options:
    """Placements export options. map_filter: map sub-path ("Maps/<Map>/") or a path substring."""
    map_filter: str
    out: str = ""                     # default: out/map/<Name>_placements.jsonl

    def __post_init__(self):
        if not self.out:
            self.out = os.path.join(HERE, "out", "map",
                                    self.map_filter.strip("/").split("/")[-1] + "_placements.jsonl")

# ---------- run state (filled by run()) ------------------------------------------------
FILTER = ""
OUT = ""
T = None                              # game.Templates() of the current run
cnt = collections.Counter()
why = collections.Counter()
classes = collections.Counter()
MB = {}                               # mesh key -> {"bounds": 7d} | {"err": ...}

def reset(opt: Options):
    """Fresh state for one run (a process may export several maps in a row)."""
    global FILTER, OUT, T, cnt, why, classes, MB
    FILTER, OUT = opt.map_filter, opt.out
    T = game.templates()              # shared: packages stay parsed across steps
    cnt, why, classes = collections.Counter(), collections.Counter(), collections.Counter()
    MB = {}

# ---------- pure helpers ----------------------------------------------------------------
def rel_of(fl):
    """Relative* -> Transform; missing/ZERO fields use the defaults (loc 0, rot 0, scale 1)."""
    loc, rot, scl = (fl.get(k) for k in ("RelativeLocation", "RelativeRotation", "RelativeScale3D"))
    return ut.Transform.from_relative(
        None if loc in (None, "ZERO") else tuple(loc),
        None if rot in (None, "ZERO") else tuple(rot),
        None if scl in (None, "ZERO") else tuple(scl))

def rel_vals(fl):
    """Relative* as raw triples for the output (defaults 0/0/1) — not the world transform."""
    out = []
    for k, d in (("RelativeLocation", (0.0, 0.0, 0.0)), ("RelativeRotation", (0.0, 0.0, 0.0)),
                 ("RelativeScale3D", (1.0, 1.0, 1.0))):
        v = fl.get(k)
        out.append(list(d) if v in (None, "ZERO") else [float(x) for x in v])
    return out

def xform_box(M, o, ext):
    return ([M[i][0] * o[0] + M[i][1] * o[1] + M[i][2] * o[2] + M[i][3] for i in range(3)],
            [abs(M[i][0]) * ext[0] + abs(M[i][1]) * ext[1] + abs(M[i][2]) * ext[2] for i in range(3)])

def union_box(boxes):
    mins = [min(o[k] - e[k] for o, e in boxes) for k in range(3)]
    maxs = [max(o[k] + e[k] for o, e in boxes) for k in range(3)]
    return ([(mins[k] + maxs[k]) / 2 for k in range(3)], [(maxs[k] - mins[k]) / 2 for k in range(3)])

def bounds_check(Ms, mb, sb):
    """(ΔOrigin, ΔExtent, ok) — mesh bounds mapped through the world matrices, unioned over instances."""
    o, e = union_box([xform_box(M, mb[0:3], mb[3:6]) for M in Ms])
    so, se = sb[0:3], sb[3:6]
    do = max(abs(o[k] - so[k]) for k in range(3))
    de = max(abs(e[k] - se[k]) for k in range(3))
    tol = 1.0 + 1e-3 * max(max(abs(x) for x in se), 1.0)
    return do, de, do <= tol and de <= tol

# ---------- export steps ------------------------------------------------------------------
def world_of(raw, pkg, e, wc, depth=0):
    """(Transform, chain depth). AttachParent is taken only from the export itself (cooked instances
    set it locally; a template's attach points into a foreign package and is useless)."""
    if e.index in wc:
        return wc[e.index]
    fl = T.fields(raw, pkg, e)
    rel = rel_of(fl)
    r = fl.get("AttachParent")          # Templates.fields already yields ("export", i) | ("import", …)
    if r is None or depth >= 12:
        wc[e.index] = (rel, 0)
        return wc[e.index]
    if r[0] != "export" or not (0 <= r[1] < len(pkg.exports)):
        cnt[f"AttachParent {r[0]}"] += 1
        wc[e.index] = (rel, 0)
        return wc[e.index]
    par, d = world_of(raw, pkg, pkg.exports[r[1]], wc, depth + 1)
    pfl = T.fields(raw, pkg, pkg.exports[r[1]])
    if not pfl.get("_ok"):
        if any(k in pfl for k in ("RelativeLocation", "RelativeRotation", "RelativeScale3D")):
            cnt["Eltern-Schwanz verfehlt, Relative* genutzt"] += 1
        else:
            cnt["Eltern ohne Relative* (Identität)"] += 1
            why[f"Elternklasse {pfl.get('_class')}"] += 1
    wc[e.index] = (rel * par, d + 1)
    return wc[e.index]

def mesh_of(raw, pkg, e, fl, cellpath):
    """-> (key, raw2, pkg2, export) | None. key: ("/Game/…", hash) | ("local", cell, index)."""
    r = fl.get("StaticMesh")
    if r is None:
        return None
    if r[0] == "export":
        return ("local", cellpath, r[1]), raw, pkg, pkg.exports[r[1]]
    if r[0] == "import":
        pp = T.package(r[1])
        if pp is None:
            cnt["Mesh-Paket nicht im Index"] += 1
            why[f"fehlendes Mesh-Paket {r[1]}"] += 1
            return None
        me = pp[2].get(r[2])
        if me is None:
            cnt["PublicExportHash im Mesh-Paket nicht gefunden"] += 1
            return None
        return (r[1], r[2]), pp[0], pp[1], me
    cnt[f"Mesh-Referenz {r[0]}"] += 1
    return None

def mesh_bounds(key, raw2, pkg2, me):
    if key in MB:
        return MB[key]
    a = pkg2.header_size + me.offset
    try:
        _a, q = native_start(raw2, pkg2, me)
        st = mesh.walk_native(raw2, q, a + me.size)
        MB[key] = {"bounds": st["bounds"], "nlod": len(st["lods"])}
    except Exception as ex:
        MB[key] = {"err": f"{type(ex).__name__}: {str(ex)[:60]}"}
    return MB[key]

def run(filter_sub, out=None) -> str:
    """Write the placements JSONL of one map and print the accounting. Returns the output path.
    filter_sub: map sub-path ("Maps/<Map>/") or any path substring."""
    reset(Options(map_filter=filter_sub, out=out or ""))
    ctx = game.context()
    idx = iostore.path_index(ctx.C, exts=(".umap",))
    cells = sorted(p for p in idx if FILTER in p)
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    devs, ctrl_devs = [], []

    with open(OUT, "w", encoding="utf-8") as out:
        for n_zelle, p in enumerate(cells):
            report_progress(n_zelle, len(cells))
            c, ci = idx[p][0]
            raw = c.get_chunk(ci)
            pkg = zen.parse_uasset(raw, global_names=ctx.GN)
            cnt["Zellen"] += 1
            wc = {}
            for e in pkg.exports:
                cls = pkg.export_class(e, script_names=ctx.smap)
                if "StaticMeshComponent" not in chain_names(cls):
                    continue
                cnt["Komponenten"] += 1
                classes[cls] += 1
                fl = T.fields(raw, pkg, e)
                if not fl.get("_ok"):
                    cnt["(a) Leser/Ende nicht exakt"] += 1
                    why[cls] += 1
                    continue
                m = mesh_of(raw, pkg, e, fl, p)
                if m is None:
                    cnt["kein Mesh aufgelöst"] += 1
                    continue
                key, raw2, pkg2, me = m
                mcls = pkg2.export_class(me, script_names=ctx.smap)
                cnt[f"(e) Mesh-Klasse {mcls}"] += 1
                if mcls != "StaticMesh":
                    continue
                mb = mesh_bounds(key, raw2, pkg2, me)
                if "err" in mb:
                    cnt["Mesh-Bounds: walk_native fehlgeschlagen"] += 1
                    why[f"mesh {key}: {mb['err']}"] += 1
                    continue
                mb = mb["bounds"]
                W, tiefe = world_of(raw, pkg, e, wc)
                hidden = fl.get("bHiddenInGame") == 1 or fl.get("bVisible") == 0
                socket = fl.get("AttachSocketName") is not None
                cnt["versteckt" if hidden else "sichtbar"] += 1
                if socket:
                    cnt["AttachSocketName gesetzt"] += 1
                sb = fl.get("bounds")
                Wm = W.matrix()
                rv = rel_vals(fl)
                relm = rel_of(fl).matrix()   # control: without the parent chain

                def emit(mat, ism_i, Ms, ctrlMs):
                    garbage = max(abs(mat[i][3]) for i in range(3)) > 2e6
                    rec = {"cell": p, "name": e.name, "class": cls,
                           "mesh": {"path": key[0], "hash": key[1]} if key[0] != "local"
                                   else {"local_cell": key[1], "export": key[2]},
                           "mesh_name": me.name,
                           "world": [v for row in mat for v in row],
                           "rel": {"loc": rv[0], "rot": rv[1], "scale": rv[2]},
                           "ism": ism_i, "tiefe": tiefe, "hidden": hidden, "socket": socket,
                           "garbage": garbage, "spline": "SplineMeshComponent" in chain_names(cls),
                           "materials": fl.get("OverrideMaterials") or []}
                    if rec["spline"]:
                        # Deformation along the cooked SplineParams (CDO defaults filled in);
                        # arcpak.mapglb bends the mesh with them
                        rec["spline_params"] = mesh.params(fl)
                    if sb:
                        rec["bounds"] = list(sb)
                        do, de, ok = bounds_check(Ms, mb, sb)
                        rec["bc"], rec["bc_do"], rec["bc_de"] = "ok" if ok else "fail", do, de
                        cdo, cde, cok = bounds_check(ctrlMs, mb, sb) if tiefe else (None, None, None)
                        if tiefe:
                            rec["bc_ctrl"] = "ok" if cok else "fail"
                            ctrl_devs.append((cdo, cde))
                            cnt["(f) Kontrolle ohne Eltern: " + ("FAIL wie erwartet" if not cok else "TRIFFT TROTZDEM")] += 1
                        devs.append((do, de))
                        cnt[f"(f) Bounds {rec['bc']}" + (" je Instanz" if ism_i is not None else "")] += 1
                    out.write(json.dumps(rec) + "\n")
                    cnt["Platzierungen" + (" (ISM)" if ism_i is not None else "")] += 1

                if "instances" in fl:
                    off, size, n = fl["instances"]
                    if size != 128:
                        cnt[f"ISM Elementgröße {size}"] += 1
                        continue
                    cnt["ISM Komponenten"] += 1
                    Ms, CMs = [], []
                    for i in range(n):
                        I = ut.fmatrix_rows_to_cols(struct.unpack_from("<16d", raw, off + size * i))
                        Ms.append(ut.matmul(Wm, I))
                        CMs.append(ut.matmul(relm, I))
                    for i in range(n):
                        emit(Ms[i], i, Ms, CMs)
                else:
                    emit(Wm, None, [Wm], [relm])
            T.forget(pkg)          # drop the cell's field cache (id(pkg) may be reused)

    report(devs, ctrl_devs)
    return OUT

def report(devs, ctrl_devs):
    for k in sorted(cnt):
        print(f"  {cnt[k]:8,}  {k}")
    print("Klassen:", classes.most_common(10))
    print("Mesh-Pakete geladen:", len([v for v in T.pk.values() if v]), "davon verfehlt:",
          len([v for v in T.pk.values() if v is None]))
    if devs:
        dos = sorted(d[0] for d in devs); des = sorted(d[1] for d in devs)
        print(f"(f) ΔOrigin  median {dos[len(dos)//2]:.4f}  max {dos[-1]:.4f} cm")
        print(f"(f) ΔExtent  median {des[len(des)//2]:.4f}  max {des[-1]:.4f} cm")
    if ctrl_devs:
        dos = sorted(max(c) for c in ctrl_devs)
        print(f"(f) Kontrolle (Elternkomponenten, ohne Kette): Δ median {dos[len(dos)//2]:.1f}  max {dos[-1]:.1f} cm")
    if why:
        print("Gründe:")
        for k, v in why.most_common(12):
            print(f"  {v:7,}  {k}")
    print("geschrieben:", OUT)

# ---------- command line ------------------------------------------------------------------
def parse_options(argv) -> Options:
    args = list(argv)
    positional = [a for a in args if not a.startswith("--")]
    if not positional:
        raise SystemExit("placements braucht einen Map-Pfad, z. B. Maps/PracticeRangeArray/")
    out = next((a[6:] for a in args if a.startswith("--out=")), "")
    return Options(map_filter=positional[0], out=out)

def main(argv=None) -> str:
    o = parse_options(sys.argv[1:] if argv is None else argv)
    return run(o.map_filter, o.out)   # explicit: run(filter_sub, out)

if __name__ == "__main__":
    main()
