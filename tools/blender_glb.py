#!/usr/bin/env python3
"""Blender helpers for map GLBs: simplify polygons, open a GLB in Blender.

    python tools/blender_glb.py simplify <in.glb> <out.glb> [ratio=0.5] [min_tris=300]
    python tools/blender_glb.py open <file.glb> [focus words ...]

The same file runs inside Blender (`blender ... --python tools/blender_glb.py -- <mode> ...`):
there it does the actual work; outside Blender it prepares the data and starts Blender.

simplify: loading a map GLB directly would create one Blender object per placement (150 000
nodes -> 30+ minutes, 7+ GB), but decimation works per mesh datablock. So:
  1) write a "pool" GLB with the same buffers and exactly one identity node per glTF mesh
     (mesh named m<index>, node carries the original name so play-area markers can be skipped)
  2) run Blender headless on the pool: Decimate (Collapse) once per mesh datablock
  3) put the decimated primitives of every m<i> back into mesh i of the original GLB, mapping
     materials by name; nodes, matrices, scene and materials of the original stay untouched
"""
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROGRESS = "@@PROGRESS"          # same prefix as arcpak.config.report_progress
# objects never decimated: play-area markers drawn by the exporter
SKIP_NAMES = ("Spielbereich", "BlockingVolume", "PlayableAreaLimiter")


# ---------- inside Blender ------------------------------------------------------------
def blender_simplify(src, dst, ratio, min_tris):
    import bpy
    t0 = time.time()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=src)
    print(f"Import {src}: {len(bpy.data.objects)} Objekte, {len(bpy.data.meshes)} Meshes ({time.time() - t0:.1f}s)")

    def tris(me):
        return sum(len(p.vertices) - 2 for p in me.polygons)

    users = {}
    for o in bpy.data.objects:
        if o.type == "MESH":
            users.setdefault(o.data, []).append(o)
    scene_before = sum(tris(o.data) for o in bpy.data.objects if o.type == "MESH")
    pool_before = pool_after = 0
    n_dec = n_skip_mark = n_skip_small = 0
    # Evaluate Decimate on an empty scene: otherwise every step re-evaluates the depsgraph of
    # all objects (quadratic, 17 minutes for ~10 000 meshes). Unlink all objects temporarily.
    linked = [(c, o) for c in [bpy.context.scene.collection, *bpy.data.collections] for o in list(c.objects)]
    for c, o in linked:
        c.objects.unlink(o)
    last = 0.0
    for i, (me, objs) in enumerate(users.items()):
        if time.time() - last > 0.2:
            last = time.time()
            print(f"{PROGRESS} {i} {len(users)}", flush=True)
        t = tris(me)
        pool_before += t
        if any(o.name.startswith(SKIP_NAMES) for o in objs):
            n_skip_mark += 1
            pool_after += t
            continue
        if t < min_tris:
            n_skip_small += 1
            pool_after += t
            continue
        tmp = bpy.data.objects.new("_dec", me)
        bpy.context.scene.collection.objects.link(tmp)
        mod = tmp.modifiers.new("dec", "DECIMATE")
        mod.decimate_type = "COLLAPSE"
        mod.ratio = ratio
        mod.use_collapse_triangulate = True
        dg = bpy.context.evaluated_depsgraph_get()
        new = bpy.data.meshes.new_from_object(tmp.evaluated_get(dg), preserve_all_data_layers=True, depsgraph=dg)
        new.name = me.name
        bpy.data.objects.remove(tmp)
        for o in objs:
            o.data = new
        pool_after += tris(new)
        n_dec += 1
    for c, o in linked:
        c.objects.link(o)
    scene_after = sum(tris(o.data) for o in bpy.data.objects if o.type == "MESH")
    print(f"Meshes vereinfacht {n_dec}, Markierungen unverändert {n_skip_mark}, klein (<{min_tris}) unverändert {n_skip_small}")
    print(f"Dreiecke Mesh-Vorrat: {pool_before:,} → {pool_after:,} ({pool_after / max(pool_before, 1):.1%})")
    print(f"Dreiecke Szene (mit Instanzen): {scene_before:,} → {scene_after:,} ({scene_after / max(scene_before, 1):.1%})")
    bpy.ops.export_scene.gltf(filepath=dst, export_format="GLB", export_apply=False, export_materials="EXPORT",
                              export_texcoords=True, export_normals=True)
    print(f"geschrieben: {dst} ({time.time() - t0:.1f}s)")


def blender_open(glb, keys):
    """Import a GLB and frame the view. With focus words: hide backdrops and select meshes whose
    datablock name contains a word (object names are component names, not asset names)."""
    import bpy
    from mathutils import Vector
    bpy.ops.import_scene.gltf(filepath=glb)
    hidden = sel = 0
    bpy.ops.object.select_all(action="DESELECT")
    if keys:
        for obj in bpy.data.objects:
            if "Backdrop" in getattr(obj.data, "name", ""):
                obj.hide_set(True)
                hidden += 1
        for obj in bpy.data.objects:
            if any(k in getattr(obj.data, "name", "") for k in keys):
                obj.select_set(True)
                sel += 1
    else:
        # Without focus words the landscape defines the frame (terrain + surroundings), otherwise
        # the core around the median. view_all alone zooms out to far-away backdrop outliers.
        terr = next((o for o in bpy.data.objects if o.type == "MESH" and "Landscape" in o.data.name), None)
        if terr:
            mn, mx = Vector((1e9,) * 3), Vector((-1e9,) * 3)
            for corner in terr.bound_box:
                w = terr.matrix_world @ Vector(corner)
                mn = Vector(map(min, mn, w)); mx = Vector(map(max, mx, w))
            tc = (mn + mx) / 2
            terr.select_set(True); sel = 1
            for o in bpy.data.objects:
                if o.type == "MESH" and o is not terr and (o.matrix_world.translation - tc).length <= 800.0:
                    o.select_set(True)
                    sel += 1
        else:
            objs = [o for o in bpy.data.objects if o.type in {"MESH", "EMPTY"}]
            cents = [o.matrix_world.translation for o in objs]
            if cents:
                med = Vector(sorted(c[i] for c in cents)[len(cents) // 2] for i in range(3))
                for o, c in zip(objs, cents):
                    if (c - med).length <= 2000.0:
                        o.select_set(True)
                        sel += 1
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                for region in area.regions:
                    if region.type == "WINDOW":
                        with bpy.context.temp_override(window=win, area=area, region=region):
                            if bpy.context.selected_objects:
                                bpy.ops.view3d.view_selected()
                            else:
                                bpy.ops.view3d.view_all()
    print(f"Import {glb}: {len(bpy.data.objects)} Objekte | Fokus: {sel} selektiert, {hidden} Backdrop ausgeblendet")


# ---------- GLB helpers (outside Blender) ------------------------------------------------
def read_glb(path):
    with open(path, "rb") as f:
        data = f.read()
    magic, _ver, total = struct.unpack_from("<III", data, 0)
    assert magic == 0x46546C67, "not a GLB"
    p, js, binc = 12, None, b""
    while p < total:
        ln, typ = struct.unpack_from("<II", data, p)
        chunk = data[p + 8:p + 8 + ln]
        if typ == 0x4E4F534A:
            js = json.loads(chunk)
        elif typ == 0x004E4942:
            binc = chunk
        p += 8 + ln
    return js, binc


def write_glb(path, js, binc):
    jb = json.dumps(js, separators=(",", ":")).encode()
    jb += b" " * (-len(jb) % 4)
    binc += b"\0" * (-len(binc) % 4)
    total = 12 + 8 + len(jb) + 8 + len(binc)
    with open(path, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, total))
        f.write(struct.pack("<II", len(jb), 0x4E4F534A)); f.write(jb)
        f.write(struct.pack("<II", len(binc), 0x004E4942)); f.write(binc)


def tri_count(js, mesh):
    n = 0
    for pr in mesh["primitives"]:
        if pr.get("mode", 4) != 4:
            continue
        acc = pr["indices"] if "indices" in pr else pr["attributes"]["POSITION"]
        n += js["accessors"][acc]["count"] // 3
    return n


def mesh_bbox(js, mesh):
    lo, hi = [1e30] * 3, [-1e30] * 3
    for pr in mesh["primitives"]:
        a = js["accessors"][pr["attributes"]["POSITION"]]
        if "min" in a:
            lo = [min(lo[k], a["min"][k]) for k in range(3)]
            hi = [max(hi[k], a["max"][k]) for k in range(3)]
    return lo, hi


def compact(js, binc):
    """Keep only referenced accessors / buffer views and repack the buffer."""
    used_acc = []
    for m in js["meshes"]:
        for pr in m["primitives"]:
            used_acc += list(pr["attributes"].values())
            if "indices" in pr:
                used_acc.append(pr["indices"])
    amap, accs = {}, []
    for a in sorted(set(used_acc)):
        amap[a] = len(accs)
        accs.append(dict(js["accessors"][a]))
    vmap, views, out = {}, [], bytearray()
    for ac in accs:
        if "bufferView" not in ac:
            continue
        vi = ac["bufferView"]
        if vi not in vmap:
            v = js["bufferViews"][vi]
            off = v.get("byteOffset", 0)
            out.extend(b"\0" * (-len(out) % 4))
            nv = dict(v, byteOffset=len(out))
            out.extend(binc[off:off + v["byteLength"]])
            vmap[vi] = len(views)
            views.append(nv)
        ac["bufferView"] = vmap[vi]
    for m in js["meshes"]:
        for pr in m["primitives"]:
            pr["attributes"] = {k: amap[v] for k, v in pr["attributes"].items()}
            if "indices" in pr:
                pr["indices"] = amap[pr["indices"]]
    js["accessors"], js["bufferViews"] = accs, views
    js["buffers"] = [{"byteLength": len(out)}]
    return js, bytes(out)


def find_blender():
    sys.path.insert(0, HERE)
    from arcpak.config import find_blender as fb
    return fb()


def simplify(src, dst, ratio="0.5", min_tris="300"):
    t0 = time.time()
    js, binc = read_glb(src)
    if not js.get("meshes"):
        import shutil
        shutil.copyfile(src, dst)
        print(f"keine Meshes im GLB — unverändert kopiert: {dst}")
        return
    meshes = js["meshes"]
    # original name per mesh: first node using it (for the play-area skip list in Blender)
    node_name = {}
    for nd in js["nodes"]:
        if "mesh" in nd and nd["mesh"] not in node_name:
            node_name[nd["mesh"]] = nd.get("name", meshes[nd["mesh"]].get("name", ""))
    pool = dict(js)
    pool["meshes"] = [dict(m, name=f"m{i}") for i, m in enumerate(meshes)]
    pool["nodes"] = [{"mesh": i, "name": node_name.get(i, meshes[i].get("name", f"m{i}"))}
                     for i in range(len(meshes))]
    pool["scenes"] = [{"nodes": list(range(len(meshes)))}]
    pool["scene"] = 0
    tmp = tempfile.mkdtemp(prefix="glbsimplify_")
    p_pool, p_dec = os.path.join(tmp, "pool.glb"), os.path.join(tmp, "pool_dec.glb")
    write_glb(p_pool, pool, binc)
    print(f"Vorrat: {len(meshes)} Meshes statt {len(js['nodes'])} Knoten ({time.time() - t0:.1f}s)", flush=True)

    # stream Blender's output so its progress lines reach the GUI
    proc = subprocess.Popen([find_blender(), "--background", "--python", os.path.abspath(__file__),
                             "--", "simplify", p_pool, p_dec, ratio, min_tris],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    rest = []
    for line in proc.stdout:
        line = line.rstrip("\n")
        if line.startswith(PROGRESS):
            print(line, flush=True)
        elif line.startswith(("Import", "Meshes vereinfacht", "Dreiecke", "geschrieben")):
            print("  blender:", line, flush=True)
        else:
            rest = (rest + [line])[-60:]
    proc.wait()
    if proc.returncode != 0 or not os.path.exists(p_dec):
        print("\n".join(rest))
        raise SystemExit(f"FEHLER: Blender exit {proc.returncode}")

    dj, db = read_glb(p_dec)
    mat_idx = {}
    for i, m in enumerate(js.get("materials", [])):
        mat_idx.setdefault(m.get("name"), i)
    new_bin = bytearray(binc)
    new_bin += b"\0" * (-len(new_bin) % 4)
    view_map, acc_map = {}, {}

    def copy_view(vi):
        if vi in view_map:
            return view_map[vi]
        v = dj["bufferViews"][vi]
        off = v.get("byteOffset", 0)
        new_bin.extend(b"\0" * (-len(new_bin) % 4))
        nv = {"buffer": 0, "byteOffset": len(new_bin), "byteLength": v["byteLength"]}
        for k in ("byteStride", "target"):
            if k in v:
                nv[k] = v[k]
        new_bin.extend(db[off:off + v["byteLength"]])
        js["bufferViews"].append(nv)
        view_map[vi] = len(js["bufferViews"]) - 1
        return view_map[vi]

    def copy_acc(ai):
        if ai in acc_map:
            return acc_map[ai]
        ac = dict(dj["accessors"][ai])
        if "bufferView" in ac:
            ac["bufferView"] = copy_view(ac["bufferView"])
        js["accessors"].append(ac)
        acc_map[ai] = len(js["accessors"]) - 1
        return acc_map[ai]

    st = {"ersetzt": 0, "unverändert (fehlt im Export)": 0, "Material nicht zuordenbar": 0,
          "Bbox-Abweichung > 1 cm": 0}
    pool_before = sum(tri_count(js, m) for m in meshes)
    seen = set()
    for dm in dj.get("meshes", []):
        m = re.match(r"^m(\d+)(\.\d+)?$", dm.get("name", ""))
        if not m:
            continue
        i = int(m.group(1))
        if i in seen or not 0 <= i < len(meshes):
            continue
        seen.add(i)
        lo0, hi0 = mesh_bbox(js, meshes[i])
        prims = []
        for pr in dm["primitives"]:
            prim = {"attributes": {k: copy_acc(v) for k, v in pr["attributes"].items()
                                   if k in ("POSITION", "NORMAL", "TEXCOORD_0")}, "mode": pr.get("mode", 4)}
            if "indices" in pr:
                prim["indices"] = copy_acc(pr["indices"])
            if "material" in pr:
                name = dj["materials"][pr["material"]].get("name")
                if name in mat_idx:
                    prim["material"] = mat_idx[name]
                else:
                    st["Material nicht zuordenbar"] += 1
            prims.append(prim)
        new = {"name": meshes[i].get("name"), "primitives": prims}
        lo1, hi1 = mesh_bbox(js, new)
        # the Y-up <-> Z-up round trip through Blender must be the identity
        if max(abs(lo0[k] - lo1[k]) for k in range(3)) > 0.01 or max(abs(hi0[k] - hi1[k]) for k in range(3)) > 0.01:
            st["Bbox-Abweichung > 1 cm"] += 1
        meshes[i] = new
        st["ersetzt"] += 1
    st["unverändert (fehlt im Export)"] = len(meshes) - len(seen)
    js["buffers"] = [{"byteLength": len(new_bin)}]
    js, new_bin = compact(js, bytes(new_bin))      # drop the replaced, unreferenced data
    pool_after = sum(tri_count(js, m) for m in js["meshes"])
    scene_tris = sum(tri_count(js, js["meshes"][nd["mesh"]]) for nd in js["nodes"] if "mesh" in nd)
    write_glb(dst, js, new_bin)
    for k, v in st.items():
        print(f"  {v:8,}  {k}")
    print(f"Dreiecke Mesh-Vorrat: {pool_before:,} → {pool_after:,} ({pool_after / max(pool_before, 1):.1%})")
    print(f"Dreiecke Szene (mit Instanzen): {scene_tris:,}")
    print(f"geschrieben: {dst} {os.path.getsize(dst):,} Bytes ({time.time() - t0:.1f}s)")
    for p in (p_pool, p_dec):
        os.remove(p)
    os.rmdir(tmp)


def open_in_blender(glb, keys):
    sys.path.insert(0, HERE)
    from arcpak.config import detached_kwargs
    subprocess.Popen([find_blender(), "--python", os.path.abspath(__file__), "--", "open", glb, *keys],
                     cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **detached_kwargs())
    print(f"Blender gestartet: {glb}")


def main():
    try:
        import bpy  # noqa: F401  (only available inside Blender)
        inside = True
    except ImportError:
        inside = False
    if inside:
        args = sys.argv[sys.argv.index("--") + 1:]
        if args[0] == "simplify":
            blender_simplify(args[1], args[2], float(args[3]) if len(args) > 3 else 0.5,
                             int(args[4]) if len(args) > 4 else 300)
        elif args[0] == "open":
            blender_open(args[1], args[2:])
        return
    args = sys.argv[1:]
    if not args or args[0] not in ("simplify", "open"):
        raise SystemExit(__doc__)
    if args[0] == "simplify":
        simplify(*args[1:5])
    else:
        open_in_blender(args[1], args[2:])


if __name__ == "__main__":
    main()
