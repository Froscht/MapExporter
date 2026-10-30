"""arcpak.mapglb — builds a map GLB from the placements JSONL written by arcpak.placements.

arcpak.placements computed the world matrix of every StaticMesh component via the
AttachParent chain (ISM: one per instance) and resolved its mesh reference. This module:
    Mesh    decodes each unique mesh reference EXACTLY once (LOD0, inline or .ubulk) into one
            glTF mesh with one primitive per section (arcpak.gltf)
    Node    one per placement, matrix = to_gltf_matrix(world) (UE cm left-handed/Z-up →
            glTF m right-handed/Y-up: (x, z, y) · 0.01)
    Filter  skips hidden (bHiddenInGame) and garbage nodes (|translation| > 2e6 cm)

    python tools/mapexport.py glb <map-name> [placements.jsonl] [--crop] [--ohne-blaetter]
                                [--ohne-effekte] [--ohne-hilfsgeo] [--ohne-decals]
                                [--spielbereich] [--volumes] [--ohne-terrain]
    --spielbereich   draw the play-area carpet + fences from <map>_playarea.json
    --volumes        analysis: draw BlockingVolumes + PlayableAreaLimiter (default: off)
    --ohne-terrain   omit the landscape
    --ohne-effekte   omit effect meshes (path /Game/Pioneer/Effects/…): flat grids/cards that only
            their VFX material turns into smoke, light shafts or falling leaves — e.g.
            SM_FX_SmokeplumeGrid (smoke column on the horizon from BP_DistantArcActivityManager,
            material MI_FX_LandingSmoke_MV_6p_Stateless with flipbook texture
            T_FX_SmokePillar01_*_5x13). As plain geometry only a wall remains.
    --ohne-hilfsgeo  invisible helper geometry: light geometry and light blockers, light portals,
            sky/cloud spheres, occluders, editor meshes, volumes, and engine shapes that carry only
            debug/default materials (see is_helper)
    --ohne-decals    decal planes (stains, graffiti, cracks): without textures they are only grey
            planes floating in front of walls (see is_decal)
    --ohne-blaetter  omit sections with a leaf material (StaticMaterials name contains
            Branch/Leaf/Leaves/Foliage/Needle — trees such as Aleppo pine, stone pine, cypress,
            DeepForest, holly; "Trunk"/"Stem" sections are kept)
    Materials are named after their MaterialInstance (StaticMaterials → import path).
    --crop  keep only what lies in the play area (out/map/<map>_playarea.json; region = outer
            boundary with islands filled, +1 grid cell):
              nodes    at least one world vertex or triangle centroid inside the region AND
                       XY extent ≤ 2× play area (larger = surrounding backdrop: sky, clouds,
                       BackdropIsland — an island can reach in with a single vertex)
              terrain  per triangle (bbox); play-area volumes by their bbox
Output: out/map/<map-name>.glb

Accounting: every used placement maps to exactly one node; decoded meshes, nodes and
vertex/triangle counts are tallied and printed so they can be checked against the Blender import.

Importing this module has no side effects (no container is mounted, no command line is read).
The API is run(); main() is the CLI entry point. Run state lives in the module globals below and is
reset by run(), so one process can export several maps in a row (tools/mapexport.py karte).
"""
import sys, os, json, collections
from .config import APP_DIR
HERE = str(APP_DIR)            # writable root (repo, or the folder of MapExporter.exe)
from . import game, gltf as gltf_mod, iostore, mesh, uetransform as ut, zen
from .config import report_progress
from .game import native_start

# Options (see run()); the CLI mapping lives in parse_options().

NAME = ""                             # set by run(): map name (and default file stems)
SRC = ""                              # placements JSONL
OUT = ""                              # output GLB
T = None                              # game.Templates(), set by run()
cnt = collections.Counter()
why = collections.Counter()

umap_idx = {}                         # filled by run(): .umap path index
ubulk_idx = {}                        # filled by run(): .ubulk path index
bulks = collections.defaultdict(list)  # package stem -> [(container, entry id)], filled by run()

def reset():
    """Fresh state for one run (a process may export several maps in a row)."""
    global T, cnt, why, glb, umap_idx, ubulk_idx, bulks, MESH_CACHE, GEO, GLTF, LOCAL, NUR_BLATT
    global BLATT_MATS, EFFEKT_MESHES, HILFS_MESHES, DECAL_MESHES, REGION, REGION_EXTRA, REGION_SUM
    T = game.templates()          # shared: packages stay parsed across steps
    cnt, why = collections.Counter(), collections.Counter()
    glb = gltf_mod.GlbBuilder()
    MESH_CACHE, GEO, GLTF, LOCAL, NUR_BLATT = {}, {}, {}, {}, set()
    BLATT_MATS = collections.Counter()
    EFFEKT_MESHES = collections.Counter()
    HILFS_MESHES = collections.Counter()
    DECAL_MESHES = collections.Counter()
    REGION = None
    REGION_EXTRA = [None, None]
    REGION_SUM = [None, None]
    umap_idx = iostore.path_index(game.C, exts=(".umap",))
    ubulk_idx = iostore.path_index(game.C, exts=(".ubulk",))
    bulks = collections.defaultdict(list)
    for p, cl in ubulk_idx.items():
        bulks[game.stem_of(p)].extend(cl)


def lod_data(lod, raw, m, blob):
    """(bytes, buffers) for one LOD, or (None, reason) if it cannot be read."""
    if lod["kind"] == "inline":
        return raw, lod["buffers"]
    if lod["kind"] != "bulk":
        return None, "leer"
    o, _d, size, flags, _ci = m[lod["bulk"]]
    if not flags & mesh.SEP or blob is None:
        return None, "Bulk-Eintrag nicht separat"
    blob = blob[o:o + size]
    w = mesh.Walk(blob, 0, len(blob))
    b = mesh.read_buffers(w, len(lod["sections"]))
    if w.p != len(blob):
        return None, f"C0 Blob {len(blob)} verbraucht {w.p}"
    return blob, b


def mesh_source(rm):
    """JSONL mesh field -> (raw, pkg, export, ubulk stem) | None."""
    if "path" in rm:
        pp = T.package(rm["path"])
        if pp is None:
            return None
        me = pp[2].get(rm["hash"])
        if me is None:
            return None
        k = T.by_game.get(rm["path"].lower())
        return pp[0], pp[1], me, game.stem_of(k) if k else None
    c, ci = umap_idx[rm["local_cell"]][0]
    raw = c.get_chunk(ci)
    pkg = zen.parse_uasset(raw, global_names=game.GN)
    return raw, pkg, pkg.exports[rm["export"]], game.stem_of(rm["local_cell"])


glb = gltf_mod.GlbBuilder()           # replaced by reset()
MESH_CACHE = {}                      # JSONL mesh key -> key (decoded) | None
GEO = {}                             # JSONL mesh key -> (name, pos, idx, sections, nrm, uv, bounds, mats)
GLTF = {}                            # JSONL mesh key -> glTF mesh index (created on first node)
BLATT_MATS = collections.Counter()   # removed leaf materials (--ohne-blaetter)

# Options, set by run() (see parse_options / the module docstring)
CROP = False
SPIELBEREICH = False
VOLUMES = False
TERRAIN = True
OHNE_BLAETTER = False
OHNE_EFFEKTE = False
EFFEKT_PFAD = "/Game/Pioneer/Effects/"
OHNE_HILFSGEO = False
OHNE_DECALS = False
EFFEKT_MESHES = collections.Counter()      # removed effect meshes (--ohne-effekte)
HILFS_MESHES = collections.Counter()
DECAL_MESHES = collections.Counter()

# Helper meshes that are never rendered in game (or only feed lighting / visibility).
HELPER_PATHS = (
    "/Game/Pioneer/Lighting/Meshes/SM_LightGeo_",          # light geometry, M_LightGeo_Master_01
    "/Game/Pioneer/Lighting/Meshes/SM_Plane_LightPortal",  # light portals
    "/Game/Pioneer/Lighting/Meshes/Clouds/",               # cloud cards
    "/Game/Pioneer/Lighting/VolumeClouds/",                # sky / cloud spheres
    "/Engine/MapTemplates/Sky/",                           # sky sphere
    "/Game/Pioneer/Environment/Toolkit/ProcStructures/SM_LightBlock",  # light blockers
    "/Game/Pioneer/Environment/Toolkit/SM_OccluderPlane",  # visibility occluders
    "/Engine/InteractiveToolsFramework/",                  # editor gizmos
    "/Engine/EditorMeshes/", "/Engine/EngineMeshes/",      # camera / arrow / billboard meshes
    "/Game/Pioneer/Environment/Props/Interactables/Harvester_01/SM_Harvester_01_Volume_",
)
# Materials that mark placeholder / debug geometry
HELPER_MATERIALS = ("BasicShapeMaterial", "WorldGridMaterial", "DefaultMaterial",
                    "M_DebugPreset_", "MI_ProcGrid")


def is_helper(r):
    path = (r.get("mesh") or {}).get("path") or ""
    if path.startswith(HELPER_PATHS) or (r.get("mesh_name") or "").endswith("_Occluder"):
        return True
    mats = [m for m in r.get("materials") or [] if m]
    if mats and all(m.startswith(HELPER_MATERIALS) for m in mats):
        return True
    # engine shapes without an override keep BasicShapeMaterial
    return path.startswith("/Engine/BasicShapes/") and not mats


def is_decal(r):
    path = (r.get("mesh") or {}).get("path") or ""
    if "/Decals/" in path or "DecalMesh" in (r.get("mesh_name") or ""):
        return True
    mats = [m for m in r.get("materials") or [] if m]
    return bool(mats) and all("decal" in m.lower() for m in mats)
BLATT = ("branch", "leaf", "leaves", "foliage", "needle")


def material_names(raw, pkg, me):
    """StaticMaterials[i] → name of the MaterialInstance (last part of the import path) | f"mat{i}"."""
    a = pkg.header_size + me.offset
    try:
        _q, pr = game.read_unversioned(raw, a, a + me.size, "StaticMesh")
    except Exception:
        return []
    out = []
    for i, sm in enumerate(dict(pr).get("StaticMaterials") or []):
        mi = dict(sm).get("MaterialInterface")
        rf = game.ref(pkg, mi) if mi not in (None, "ZERO") else None
        out.append(rf[1].rsplit("/", 1)[-1] if isinstance(rf, tuple) and len(rf) > 1
                   and isinstance(rf[1], str) else f"mat{i}")
    return out
REGION = None                        # (cell mask array, x0, y0, cell size) with --crop
REGION_EXTRA = [None, None]          # (region +1 cell, region bbox x0,y0,x1,y1)
REGION_SUM = [None, None]            # integral images (zero border) of REGION[0] and REGION_EXTRA[0]


def _integral(a):
    """Prefix sums with a zero row/column: rect sum = S[b1+1,a1+1] - S[b0,a1+1] - S[b1+1,a0] + S[b0,a0]."""
    import numpy as np
    S = np.zeros((a.shape[0] + 1, a.shape[1] + 1), np.int64)
    S[1:, 1:] = np.cumsum(np.cumsum(a, axis=0), axis=1)
    return S


def _rect_sum(S, j0, i0, j1, i1):
    return S[j1 + 1, i1 + 1] - S[j0, i1 + 1] - S[j1 + 1, i0] + S[j0, i0]


def gltf_mesh(key):
    """Create the glTF mesh only when a node needs it (--crop: no orphaned meshes)."""
    if key not in GLTF:
        name, pos, idx, sections, nrm, uv, _b, mats = GEO[key]
        GLTF[key] = glb.add_mesh(name, pos, idx, sections, nrm, uv, mat_names=mats)
    return GLTF[key]


def load_region():
    import numpy as np
    src = os.path.join(HERE, "out", "map", NAME + "_playarea.json")
    if not os.path.exists(src):
        raise SystemExit(f"--crop braucht {src} (python tools/mapexport.py playarea)")
    D = json.load(open(src))
    x0, y0, nx, ny = D["raster"]
    R = np.zeros((ny, nx), bool)
    for j, i0, n in D["region"]:
        R[j, i0:i0 + n] = True
    return R, x0, y0, D["zelle_cm"]


def in_region(lo, hi):
    """Does the world bbox (UE cm) touch the region (with a 1-cell margin)?"""
    R, x0, y0, c = REGION
    ny, nx = R.shape
    i0 = max(int((lo[0] - x0) // c) - 1, 0); i1 = min(int((hi[0] - x0) // c) + 1, nx - 1)
    j0 = max(int((lo[1] - y0) // c) - 1, 0); j1 = min(int((hi[1] - y0) // c) + 1, ny - 1)
    if i0 > i1 or j0 > j1:
        return False
    S = REGION_SUM[0]
    if S is not None:                       # integral image: rect sum > 0 is exactly .any() on the rect
        return bool(_rect_sum(S, j0, i0, j1, i1) > 0)
    return bool(R[j0:j1 + 1, i0:i1 + 1].any())


def region_keep_mask(lo_a, hi_a):
    """Vectorized in_region() for many bboxes ((n, 3) numpy arrays). Exact: fast decisions come from
    the integral image over each bbox's cell rect, widened by one cell beyond in_region's margined rect
    — a superset of it (absorbs any off-by-one between numpy and Python floordiv) — so "all cells set"
    implies in_region's any() and "no cell set" implies its negation. Only bboxes straddling the region
    border are tested one by one with in_region()."""
    import numpy as np
    R, x0, y0, c = REGION
    S = REGION_SUM[0] if REGION_SUM[0] is not None else _integral(R)
    ny, nx = R.shape
    i0 = np.floor_divide(lo_a[:, 0] - x0, c).astype(np.int64) - 2     # in_region's -1 margin + 1 cell slack
    i1 = np.floor_divide(hi_a[:, 0] - x0, c).astype(np.int64) + 2
    j0 = np.floor_divide(lo_a[:, 1] - y0, c).astype(np.int64) - 2
    j1 = np.floor_divide(hi_a[:, 1] - y0, c).astype(np.int64) + 2
    a0 = np.clip(i0, 0, nx - 1); a1 = np.clip(i1, 0, nx - 1)
    b0 = np.clip(j0, 0, ny - 1); b1 = np.clip(j1, 0, ny - 1)
    empty = (i1 < 0) | (i0 > nx - 1) | (j1 < 0) | (j0 > ny - 1) | (a0 > a1) | (b0 > b1)
    tot = _rect_sum(S, b0, a0, b1, a1)
    keep = (~empty) & (tot == (a1 - a0 + 1) * (b1 - b0 + 1))
    straddle = (~keep) & (~empty) & (tot > 0)
    if straddle.any():
        lo_l, hi_l = lo_a.tolist(), hi_a.tolist()
        for k in np.flatnonzero(straddle):
            keep[k] = in_region(lo_l[k], hi_l[k])
    return keep


LOCAL = {}                           # mesh key -> (vertices, centroids) as numpy (--crop)


def node_in_region(key, M, pos=None, wbox=None):
    """--crop rule for map nodes (see module docstring). pos: bent spline vertices instead of GEO.
    wbox: the caller's world bbox (world_aabb of the local bbox) — allows an exact shortcut."""
    import numpy as np
    R, x0, y0, c = REGION
    D, RB = REGION_EXTRA
    SD = REGION_SUM[1]
    if wbox is not None and SD is not None:
        # Shortcut: every vertex (and every triangle centroid, being convex) lies inside wbox, the
        # conservative rotated-box bounds of the local bbox. Cell math in numpy floordiv — like the
        # vertex math below — keeps the widened cell rect a superset of all vertex cells; if it lies
        # fully inside the grid and all its dilated cells are set, the vertex test must pass. The
        # Kulisse test cannot fire either: the tight box extent ≤ wbox extent ≤ limit - 1e-6 leaves
        # more slack (1e-6 cm) than any float roundoff at map scale (≤1e-10 cm).
        wlo, whi = wbox
        ny, nx = D.shape
        i0 = int((np.float64(wlo[0]) - x0) // c) - 1; i1 = int((np.float64(whi[0]) - x0) // c) + 1
        j0 = int((np.float64(wlo[1]) - y0) // c) - 1; j1 = int((np.float64(whi[1]) - y0) // c) + 1
        if (max(whi[0] - wlo[0], whi[1] - wlo[1]) + 1e-6 < 2 * max(RB[2] - RB[0], RB[3] - RB[1])
                and i0 >= 0 and j0 >= 0 and i1 < nx and j1 < ny
                and _rect_sum(SD, j0, i0, j1, i1) == (i1 - i0 + 1) * (j1 - j0 + 1)):
            return True
    if pos is None:
        if key not in LOCAL:
            P = np.asarray(GEO[key][1]); I = np.asarray(GEO[key][2])
            LOCAL[key] = np.vstack([P, P[I.reshape(-1, 3)].mean(1)]) if len(I) % 3 == 0 else P
        pts = LOCAL[key]
    else:
        P = np.asarray(pos); I = np.asarray(GEO[key][2])
        pts = np.vstack([P, P[I.reshape(-1, 3)].mean(1)])
    A = np.array(M)[:3]
    W = pts @ A[:, :3].T + A[:, 3]
    lo, hi = W.min(0), W.max(0)
    if max(hi[0] - lo[0], hi[1] - lo[1]) > 2 * max(RB[2] - RB[0], RB[3] - RB[1]):
        cnt["Zuschnitt: Kulisse > 2× Spielbereich entfernt"] += 1
        return False
    i = ((W[:, 0] - x0) // c).astype(int); j = ((W[:, 1] - y0) // c).astype(int)
    ok = (i >= 0) & (i < R.shape[1]) & (j >= 0) & (j < R.shape[0])
    return bool(D[j[ok], i[ok]].any())


def world_aabb(M, lo, hi):
    """Local bbox → world bbox via the row-major UE matrix M (4×4)."""
    o = [(lo[k] + hi[k]) / 2 for k in range(3)]
    e = [(hi[k] - lo[k]) / 2 for k in range(3)]
    c = [M[i][0] * o[0] + M[i][1] * o[1] + M[i][2] * o[2] + M[i][3] for i in range(3)]
    w = [abs(M[i][0]) * e[0] + abs(M[i][1]) * e[1] + abs(M[i][2]) * e[2] for i in range(3)]
    return [c[k] - w[k] for k in range(3)], [c[k] + w[k] for k in range(3)]


NUR_BLATT = set()          # mesh keys dropped entirely by --ohne-blaetter


def mesh_index(rm):
    key = (rm.get("path"), rm.get("hash"), rm.get("local_cell"), rm.get("export"))
    if key in MESH_CACHE:
        return MESH_CACHE[key]
    src = mesh_source(rm)
    if src is None:
        cnt["Mesh-Paket nicht ladbar"] += 1
        MESH_CACHE[key] = None
        return None
    raw, pkg, me, stem = src
    a = pkg.header_size + me.offset
    try:
        _a, q = native_start(raw, pkg, me)
        st = mesh.walk_native(raw, q, a + me.size)
    except Exception as ex:
        cnt["walk_native fehlgeschlagen"] += 1
        why[f"{type(ex).__name__}: {str(ex)[:60]}"] += 1
        MESH_CACHE[key] = None
        return None
    blob = None
    if stem in bulks:
        c, ud = bulks[stem][0]
        blob = c.get_chunk(ud)
    m = pkg.bulk_data_map()
    for lodi, lod in enumerate(st["lods"]):
        d, b = lod_data(lod, raw, m, blob)
        if d is None:
            cnt[f"LOD übergangen ({b[:20] if isinstance(b, str) else b})"] += 1
            continue
        pos = mesh.positions(d, b)
        idx = mesh.indices(d, b["index"])
        nrm = mesh.normals(d, b)
        uv = mesh.uvs(d, b, 0)
        mats = material_names(raw, pkg, me)
        secs = lod["sections"]
        if OHNE_BLAETTER:
            weg = [s for s in secs if s[0] < len(mats) and any(k in mats[s[0]].lower() for k in BLATT)]
            if weg:
                cnt["Blätter: Meshes mit Blatt-Sektionen"] += 1
                cnt["Blätter: Sektionen entfernt"] += len(weg)
                cnt["Blätter: Dreiecke entfernt (Mesh-Vorrat)"] += sum(s[2] for s in weg)
                for s in weg:
                    BLATT_MATS[mats[s[0]]] += 1
                secs = [s for s in secs if s not in weg]
            if not secs:
                cnt["Blätter: Mesh nur aus Blättern (entfällt)"] += 1
                NUR_BLATT.add(key)
                MESH_CACHE[key] = None
                return None
        GEO[key] = (me.name, pos, idx, secs, nrm, uv, st["bounds"], mats)
        cnt["Meshes dekodiert"] += 1
        cnt["Dreiecke (Mesh-Vorrat)"] += sum(s[2] for s in lod["sections"])
        cnt["Vertices (Mesh-Vorrat)"] += len(pos)
        MESH_CACHE[key] = key
        return key
    cnt["kein dekodierbares LOD"] += 1
    MESH_CACHE[key] = None
    return None


def spline_geo(key, r):
    """SplineMeshComponent: bend vertices and normals in component space along the
    SplineParams (arcpak.mesh.deform) → (name, pos, idx, sections, nrm, uv, mats) for a separate
    glTF mesh per placement; the node matrix stays the component's world matrix."""
    name, pos, idx, sections, nrm, uv, b, mats = GEO[key]
    lo = tuple(b[k] - b[3 + k] for k in range(3))
    hi = tuple(b[k] + b[3 + k] for k in range(3))
    p2, n2 = mesh.deform(pos, nrm, r["spline_params"], lo, hi)
    cnt["Spline-Meshes gebogen"] += 1
    return name + " (Spline)", p2, idx, sections, n2, uv, mats


PA_FARBEN = {"Spielbereich Teppich": (0.15, 0.85, 0.25, 0.45),
             "Spielbereich Grenze": (0.1, 0.9, 0.2, 0.8),
             "Spielbereich Insel": (0.95, 0.15, 0.1, 0.8),
             "BlockingVolume": (1.0, 0.45, 0.1, 0.18),
             "PlayableAreaLimiter": (1.0, 0.9, 0.1, 0.9)}


def playarea():
    """Draw <name>_playarea.json from arcpak.playarea (`python tools/mapexport.py playarea`):
    a carpet on the ground (reachable grid cells), fences along the outline loops,
    BlockingVolumes as convex meshes, the PlayableAreaLimiter spline as a band. Everything is in
    world cm; nodes have no matrix."""
    src = os.path.join(HERE, "out", "map", NAME + "_playarea.json")
    if not (SPIELBEREICH or VOLUMES):
        return
    if not os.path.exists(src):
        raise SystemExit(f"--spielbereich/--volumes brauchen {src} (python tools/mapexport.py playarea)")
    D = json.load(open(src))
    for k, col in PA_FARBEN.items():
        if (k.startswith("Spielbereich") and SPIELBEREICH) or (not k.startswith("Spielbereich") and VOLUMES):
            glb.material(k, col)
    x0, y0, nx, ny = D["raster"]
    c = D["zelle_cm"]

    # carpet: corner height = mean of the adjacent reachable cells, +40 cm above ground
    zc = {}
    for j, i0, zs in D["teppich"]:
        for k, z in enumerate(zs):
            if z is not None:
                zc[(j, i0 + k)] = z
    corner, pos = {}, []

    def cv(i, j):
        if (i, j) not in corner:
            zs = [zc[q] for q in ((j - 1, i - 1), (j - 1, i), (j, i - 1), (j, i)) if q in zc]
            corner[(i, j)] = len(pos)
            pos.append((x0 + i * c, y0 + j * c, sum(zs) / len(zs) + 40.0))
        return corner[(i, j)]
    idx = []
    for (j, i) in zc:
        a, b, d, e = cv(i, j), cv(i + 1, j), cv(i + 1, j + 1), cv(i, j + 1)
        idx += [a, b, d, a, d, e]
    if idx and SPIELBEREICH:
        gi = glb.add_mesh("Spielbereich Teppich", pos, idx, [(0, 0, len(idx) // 3, 0, len(pos) - 1)],
                          [(0.0, 0.0, 1.0)] * len(pos), mat_names=["Spielbereich Teppich"])
        glb.add_node(gi, name="Spielbereich Teppich")
        cnt["Spielbereich: Teppich-Zellen"] += len(zc)

    def band(name, mat, pts, h, closed=True):
        """Vertical band (fence) along a point sequence [x, y, z|None], height h cm."""
        zs = [p[2] for p in pts if p[2] is not None]
        if len(pts) < 2 or not zs:
            return
        z_last = zs[0]
        P2, I2, N2 = [], [], []
        seq = pts + ([pts[0]] if closed else [])
        for p in seq:
            z = p[2] if p[2] is not None else z_last
            z_last = z
            P2 += [(p[0], p[1], z), (p[0], p[1], z + h)]
        for k in range(len(seq) - 1):
            a = 2 * k
            I2 += [a, a + 2, a + 3, a, a + 3, a + 1]
        for k in range(len(seq)):
            q0, q1 = seq[max(k - 1, 0)], seq[min(k + 1, len(seq) - 1)]
            dx, dy = q1[0] - q0[0], q1[1] - q0[1]
            L = (dx * dx + dy * dy) ** 0.5 or 1.0
            N2 += [(dy / L, -dx / L, 0.0)] * 2
        gi = glb.add_mesh(name, P2, I2, [(0, 0, len(I2) // 3, 0, len(P2) - 1)], N2, mat_names=[mat])
        glb.add_node(gi, name=name)

    for n, lp in enumerate(D["loops"] if SPIELBEREICH else []):
        outer = lp["art"] == "Außengrenze"
        band(f"Spielbereich {'Grenze' if outer else 'Insel'} {n} ({lp['flaeche_m2']:.0f} m²)",
             "Spielbereich Grenze" if outer else "Spielbereich Insel", lp["punkte"], 400.0)
        cnt["Spielbereich: Zäune"] += 1
    for v in D["volumes"] if VOLUMES else []:
        if v["class"] != "BlockingVolume":
            continue
        V, I = v["verts"], v["tris"]
        if CROP and not in_region([min(q[k] for q in V) for k in range(3)], [max(q[k] for q in V) for k in range(3)]):
            cnt["Zuschnitt: BlockingVolumes außerhalb entfernt"] += 1
            continue
        gi = glb.add_mesh(f"BlockingVolume {v['export']}", V, I, [(0, 0, len(I) // 3, 0, len(V) - 1)],
                          mat_names=["BlockingVolume"])
        glb.add_node(gi, name=f"BlockingVolume {v['export']}" + (" (Grenzwand)" if v.get("grenzwand") else ""))
        cnt["Spielbereich: BlockingVolumes"] += 1
    for L in D["limiter"] if VOLUMES else []:
        band(f"PlayableAreaLimiter {L['export']} (3-Punkt-Spline, nicht der Spielbereich)",
             "PlayableAreaLimiter", L["punkte"], 300.0, closed=L["looped"])
        cnt["Spielbereich: Limiter-Splines"] += 1


def landscape_root(raw, pkg, comps):
    """(RelativeLocation, RelativeScale3D) of the landscape root via the AttachParent
    chain (cell-local). Component relative locations are in quad-grid units, the root's in cm;
    UE multiplies the cooked root scale (typically 100,100,100) onto the quad grid — it is
    passed to mesh.build() as s/zs."""
    par = comps[0]["parent"]
    if isinstance(par, tuple):
        par = par[1] if par[0] == "export" else None
    loc = [0.0, 0.0, 0.0]
    scale = [1.0, 1.0, 1.0]
    seen = set()
    while isinstance(par, int) and par not in seen and 0 <= par < len(pkg.exports):
        seen.add(par)
        fl = T.fields(raw, pkg, pkg.exports[par])
        rl = fl.get("RelativeLocation") or (0.0, 0.0, 0.0)
        loc = [loc[k] + rl[k] for k in range(3)]
        sc = fl.get("RelativeScale3D")
        if sc:
            scale = list(sc)
        par = fl.get("AttachParent")
        if isinstance(par, tuple):
            par = par[1] if par[0] == "export" else None
    return tuple(loc), tuple(scale)


def landscapes():
    """LandscapeComponent height blocks → terrain grid (see arcpak.mesh).
    Tallies components, parsed height blocks, box verification, neighbour edges and
    vertices/triangles; a cell is built only if every height block is parsed and verified."""
    for p in sorted(k for k in umap_idx if NAME in k):
        c, ci = umap_idx[p][0]
        raw = c.get_chunk(ci)
        pkg = zen.parse_uasset(raw, global_names=game.GN)
        comps = mesh.components(raw, pkg, game.smap, game.read_unversioned, fields=T.fields)
        if not comps:
            continue
        cnt["Landscape-Zellen"] += 1
        cnt["Landscape-Komponenten"] += len(comps)
        ready = True
        for cp in comps:
            if cp["heights"] is None:
                cnt["Höhenblock nicht geparst"] += 1
                ready = False
                continue
            if cp["box"] is None:
                cnt["CachedLocalBox nicht gefunden"] += 1
                continue
            z1 = (min(cp["heights"]) - 32768) / 128.0
            z2 = (max(cp["heights"]) - 32768) / 128.0
            ok = abs(z1 - cp["box"][0]) < 0.01 and abs(z2 - cp["box"][1]) < 0.01
            cnt["Höhen je Box verifiziert: " + ("ok" if ok else "ABWEICHUNG")] += 1
            ready = ready and ok
        eq, tot, dsum = mesh.continuity(comps)
        cnt[f"Nachbarkanten identisch {eq}/{tot} (|Δ| {dsum})"] += 1
        if not ready:
            cnt["Terrain verworfen (Abrechnung offen)"] += 1
            continue
        root, sc = landscape_root(raw, pkg, comps)
        pos, idx, nrm, st = mesh.build(comps, root=root, s=sc[0], zs=sc[2])
        if CROP:
            # keep a triangle if its bbox touches the region; renumber vertices
            import numpy as np
            PA, NI, NA = np.asarray(pos), np.asarray(idx, dtype=np.int64), np.asarray(nrm)
            tri = NI.reshape(-1, 3)
            B = PA[tri]                                       # (ntri, 3, 3) vertex positions per triangle
            keep = region_keep_mask(B.min(1), B.max(1))
            sel = tri[keep].ravel()                           # kept vertices in triangle order
            if sel.size:
                order = np.argsort(sel, kind="stable")        # first appearance == dict insertion order
                srt = sel[order]
                neu = np.concatenate(([True], srt[1:] != srt[:-1]))
                vals = srt[neu][np.argsort(order[neu], kind="stable")]
                new_id = np.full(len(PA), -1, dtype=np.int64)
                new_id[vals] = np.arange(len(vals), dtype=np.int64)
                idx = new_id[sel]
            else:
                vals, idx = np.empty(0, np.int64), np.empty(0, np.int64)
            cnt["Zuschnitt: Terrain-Dreiecke entfernt"] += len(NI) // 3 - len(sel) // 3
            pos, nrm = PA[vals], NA[vals]
            st = dict(st, vertices=len(vals), dreiecke=len(idx) // 3)
            if len(idx) == 0:
                continue
        gi = glb.add_mesh(f"Landscape {game.stem_of(p)}", pos, idx,
                          [(0, 0, st["dreiecke"], 0, st["vertices"] - 1)], nrm)
        glb.add_node(gi, name=f"Landscape {game.stem_of(p)}")
        cnt["Terrain-Knoten"] += 1
        cnt["Terrain-Vertices"] += st["vertices"]
        cnt["Terrain-Dreiecke"] += st["dreiecke"]
        cnt["Terrain-Vertices geschweißt"] += st["geschweißt"]
        cnt["Terrain-Normale +Z"] += st["normale_+z"]


def run(name, src=None, out=None, crop=False, spielbereich=False, volumes=False, terrain=True,
        ohne_blaetter=False, ohne_effekte=False, ohne_hilfsgeo=False, ohne_decals=False) -> str:
    """Build the GLB of one map from its placements JSONL and return the output path.
    Importing this module has no side effects; run() may be called repeatedly in one process."""
    global NAME, SRC, OUT, CROP, SPIELBEREICH, VOLUMES, TERRAIN, OHNE_BLAETTER, OHNE_EFFEKTE
    global OHNE_HILFSGEO, OHNE_DECALS
    NAME = name
    SRC = src or os.path.join(HERE, "out", "map", name + "_placements.jsonl")
    OUT = out or os.path.join(HERE, "out", "map", name + ".glb")
    CROP, SPIELBEREICH, VOLUMES = bool(crop), bool(spielbereich), bool(volumes)
    TERRAIN, OHNE_BLAETTER, OHNE_EFFEKTE = bool(terrain), bool(ohne_blaetter), bool(ohne_effekte)
    OHNE_HILFSGEO, OHNE_DECALS = bool(ohne_hilfsgeo), bool(ohne_decals)
    reset()
    _run()
    return OUT

def _run():
    global REGION
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    if CROP:
        import numpy as np
        REGION = load_region()
        R, x0, y0, c = REGION
        D = R.copy()                                   # region +1 cell (vertex test)
        D[1:, :] |= R[:-1, :]; D[:-1, :] |= R[1:, :]; D[:, 1:] |= R[:, :-1]; D[:, :-1] |= R[:, 1:]
        rx, ry = np.where(R.any(0))[0], np.where(R.any(1))[0]
        REGION_EXTRA[:] = [D, (x0 + rx.min() * c, y0 + ry.min() * c, x0 + (rx.max() + 1) * c, y0 + (ry.max() + 1) * c)]
        REGION_SUM[:] = [_integral(R), _integral(D)]   # rect queries: in_region and the node shortcut
        cnt["Zuschnitt: Region-Zellen"] = int(R.sum())
    with open(SRC, encoding="utf-8") as fh:
        n_zeilen = sum(1 for _ in fh)
    for n_zeile, line in enumerate(open(SRC, encoding="utf-8")):
        report_progress(n_zeile, n_zeilen)
        r = json.loads(line)
        cnt["Platzierungen gelesen"] += 1
        if r.get("garbage"):
            cnt["übersprungen: Müll"] += 1
            continue
        if r.get("hidden"):
            cnt["übersprungen: versteckt"] += 1
            continue
        if OHNE_EFFEKTE and ((r.get("mesh") or {}).get("path") or "").startswith(EFFEKT_PFAD):
            cnt["übersprungen: Effekt-Mesh (--ohne-effekte)"] += 1
            EFFEKT_MESHES[r.get("mesh_name") or r["mesh"]["path"].rsplit("/", 1)[-1]] += 1
            continue
        if OHNE_HILFSGEO and is_helper(r):
            cnt["übersprungen: Hilfsgeometrie (--ohne-hilfsgeo)"] += 1
            HILFS_MESHES[r.get("mesh_name") or r["mesh"]["path"].rsplit("/", 1)[-1]] += 1
            continue
        if OHNE_DECALS and is_decal(r):
            cnt["übersprungen: Decal-Fläche (--ohne-decals)"] += 1
            DECAL_MESHES[r.get("mesh_name") or r["mesh"]["path"].rsplit("/", 1)[-1]] += 1
            continue
        key = mesh_index(r["mesh"])
        if key is None:
            rm = r["mesh"] or {}
            if (rm.get("path"), rm.get("hash"), rm.get("local_cell"), rm.get("export")) in NUR_BLATT:
                cnt["übersprungen: Mesh nur Blätter (--ohne-blaetter)"] += 1
            else:
                cnt["übersprungen: Mesh nicht dekodierbar"] += 1
            continue
        M = [r["world"][0:4], r["world"][4:8], r["world"][8:12], r["world"][12:16]]
        sg = spline_geo(key, r) if r.get("spline_params") else None
        if sg:
            # Python floats (not numpy scalars): in_region's // cell math must behave like before
            lo = [float(min(p[k] for p in sg[1])) for k in range(3)]
            hi = [float(max(p[k] for p in sg[1])) for k in range(3)]
        else:
            b = GEO[key][6]
            lo, hi = [b[k] - b[3 + k] for k in range(3)], [b[k] + b[3 + k] for k in range(3)]
        if CROP:
            wb = world_aabb(M, lo, hi)
            if not (in_region(*wb) and node_in_region(key, M, sg[1] if sg else None, wb)):
                cnt["Zuschnitt: Knoten außerhalb entfernt"] += 1
                continue
        gi = glb.add_mesh(*sg[:6], mat_names=sg[6]) if sg else gltf_mesh(key)
        glb.add_node(gi, name=r["name"], matrix=ut.to_gltf_matrix(M))
        cnt["Knoten gesetzt"] += 1
        if r["ism"] is not None:
            cnt["Knoten (ISM-Instanzen)"] += 1
    if TERRAIN:
        landscapes()
    playarea()
    glb.write(OUT)
    for k in sorted(cnt):
        print(f"  {cnt[k]:8,}  {k}")
    print(f"  {len(glb.meshes):8,}  glTF-Meshes   {len(glb.nodes):8,}  glTF-Knoten")
    print("geschrieben:", OUT, os.path.getsize(OUT), "Bytes")
    if BLATT_MATS:
        print("entfernte Blatt-Materialien:", ", ".join(sorted(BLATT_MATS)))
    if EFFEKT_MESHES:
        print("entfernte Effekt-Meshes:", ", ".join(f"{k} ×{v}" for k, v in EFFEKT_MESHES.most_common()))
    for title, ctr in (("entfernte Hilfsgeometrie", HILFS_MESHES), ("entfernte Decal-Flächen", DECAL_MESHES)):
        if ctr:
            print(f"{title}:", ", ".join(f"{k} ×{v}" for k, v in ctr.most_common(15))
                  + (f" … (+{len(ctr) - 15} weitere)" if len(ctr) > 15 else ""))
    if why:
        print("Gründe:")
        for k, v in why.most_common(10):
            print(f"  {v:7,}  {k}")


def parse_options(argv) -> dict:
    """Command line -> run() keyword arguments."""
    args = list(argv)
    positional = [a for a in args if not a.startswith("--") and not a.endswith(".jsonl")]
    if not positional:
        raise SystemExit("glb braucht einen Map-Namen, z. B. PracticeRangeArray")
    return {"name": positional[0],
            "src": next((a for a in args if a.endswith(".jsonl")), None),
            "out": next((a[6:] for a in args if a.startswith("--out=")), None),
            "crop": "--crop" in args,
            "spielbereich": "--spielbereich" in args,
            "volumes": "--volumes" in args,
            "terrain": "--ohne-terrain" not in args,
            "ohne_blaetter": "--ohne-blaetter" in args,
            "ohne_effekte": "--ohne-effekte" in args,
            "ohne_hilfsgeo": "--ohne-hilfsgeo" in args,
            "ohne_decals": "--ohne-decals" in args}

def main(argv=None) -> str:
    return run(**parse_options(sys.argv[1:] if argv is None else argv))

if __name__ == "__main__":
    main()
