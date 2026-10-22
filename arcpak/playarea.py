"""arcpak.playarea — walkable play area of a map, derived from blocking volumes and terrain.

Sources (all cell-local; LevelInstance transforms are treated as identity):
    BlockingVolume   Brush geometry from BrushComponent.BrushBodySetup → BodySetup.AggGeom
                     ConvexElems[0]: int32 n | n×FVector(double) | [int32 m | m×int32] | FBox.
                     The property reader truncates VertexData (shows 3), so it is read byte-exact:
                     a convex is accepted only if the following ElemBox is exactly the bbox of
                     the vertices (and IndexData, if present, is valid).
                     World = brush vertices × BrushComponent transform (AttachParent chain).
    PlayerStart      Seed of the flood fill (RootComponent location).
    Landscape        Ground height per grid cell (landscape components via arcpak.mesh).
    PlayableAreaLimiter  Documented only (SplineCurves.Position, looped) — in PracticeRangeArray it
                     is a 3-point triangle away from all game content, NOT the play area.

Play area = flood fill on an XY grid (default 100 cm) starting at the PlayerStart; a cell is
blocked if a BlockingVolume intersects the walking corridor [ground+50, ground+180] cm
(without terrain: [−2000, 8000]). Outline = edges between reachable and unreachable cells, chained
into loops and simplified with Douglas-Peucker (75 cm).
Slope rule: neighbouring cells are connected only if |Δground| ≤ cell·tan(slope) + 45 cm
(UE CharacterMovement: WalkableFloorAngle 44.765°, MaxStepHeight 45). Grid = bbox of the
BlockingVolumes ∪ NavMeshBoundsVolume (BuriedCity has BlockingVolumes only inside, no frame).

    python tools/mapexport.py playarea <map sub-path> [--cell=100] [--seed=x,y] [--strikt]
        [--hang=44.765|aus] [--dump=raster.npz]

    --strikt  do not extend boundary walls over their full height
    --hang=   max walkable slope in degrees; "aus" disables the slope rule
    --dump=   write the grid as .npz for diagnostics

Output: out/map/<Name>_playarea.json (outline, carpet runs, volumes, PlayerStart, limiters);
`python tools/mapexport.py glb` draws it / crops with it when the file exists.

Importing this module has no side effects (no container is mounted, no command line is read).
The API is run(); main() is the CLI entry point. A map without a play area raises NoPlayArea
(exit code 3), which the callers turn into "export without cropping".
"""
import sys, os, json, struct, collections, math
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from . import game, iostore, mesh, uetransform as ut, zen
from .config import report_progress

EXIT_KEIN_SPIELBEREICH = 3       # the GUI then skips this step and exports without cropping

class NoPlayArea(SystemExit):
    """No play area could be determined (exit code EXIT_KEIN_SPIELBEREICH). The caller (GUI,
    `mapexport.py all` / `karte`) then exports without cropping."""

    def __init__(self, grund):
        super().__init__(EXIT_KEIN_SPIELBEREICH)
        self.grund = grund

# Run state, filled by run(); the functions below read these module globals. Importing this module
# touches no container and no command line.
FILTER = ""
NAME = ""
CELL = 100.0
STRIKT = False                   # do not extend boundary walls over their full height
HANG = 44.765                    # max walkable slope in degrees (None = off)
SEED = None
DUMP = None
OUT = ""
T = None                         # game.Templates(), set by run()
cnt = collections.Counter()


def reset():
    """Fresh state for one run (the module stays reusable inside one process)."""
    global T, cnt
    T = game.templates()          # shared: packages stay parsed across steps
    cnt = collections.Counter()


def kein_spielbereich(grund):
    """No play area could be determined: remove any previous result (otherwise the GLB export would
    crop with stale data) and signal NoPlayArea (exit code EXIT_KEIN_SPIELBEREICH)."""
    if OUT and os.path.exists(OUT):
        os.remove(OUT)
    print(f"KEIN SPIELBEREICH: {grund}", flush=True)
    raise NoPlayArea(grund)


def cls_of(pkg, e):
    return pkg.export_class(e, script_names=game.smap) or "?"


def brush_convex(body):
    """[(offset, vertices, indices|None)] from a BodySetup body; accepted only if the ElemBox matches."""
    out = []
    for off in range(0, len(body) - 8):
        n = struct.unpack_from("<i", body, off)[0]
        if not 4 <= n <= 256 or off + 4 + 24 * n + 49 > len(body):
            continue
        V = [struct.unpack_from("<3d", body, off + 4 + 24 * i) for i in range(n)]
        # Plausibility: finite, no denormal garbage (the cooked physics payload otherwise yields
        # random hits with |x| ~ 1e-308), at least one coordinate ≥ 1 cm
        if not all(all(math.isfinite(x) and abs(x) < 1e7 and (x == 0 or abs(x) >= 1e-3) for x in v)
                   for v in V) or max(abs(x) for v in V for x in v) < 1.0:
            continue
        lo = [min(v[k] for v in V) for k in range(3)]
        hi = [max(v[k] for v in V) for k in range(3)]
        if min(hi[k] - lo[k] for k in range(3)) <= 0:
            continue
        nxt = off + 4 + 24 * n
        I = None
        m = struct.unpack_from("<i", body, nxt)[0]
        if m == 0:                                  # empty IndexData array (int32 0)
            nxt += 4
        elif 12 <= m <= 3000 and m % 3 == 0 and nxt + 4 + 4 * m + 48 <= len(body):
            J = struct.unpack_from(f"<{m}i", body, nxt + 4)
            if min(J) >= 0 and max(J) < n and len(set(J)) == n:
                I, nxt = list(J), nxt + 4 + 4 * m
        box = struct.unpack_from("<6d", body, nxt)
        if all(abs(box[k] - lo[k]) < 1e-6 and abs(box[3 + k] - hi[k]) < 1e-6 for k in range(3)):
            out.append((off, V, I))
    return out


def box_tris(V):
    """Triangles for a box from its 8 bbox corners (IndexData is missing in cooked data)."""
    lo = [min(v[k] for v in V) for k in range(3)]
    key = {tuple(int(v[k] > lo[k] + 1e-9) for k in range(3)): i for i, v in enumerate(V)}
    if len(key) != 8:
        return None
    q = lambda a, b, c, d: [key[a], key[b], key[c], key[a], key[c], key[d]]
    return (q((0, 0, 0), (0, 1, 0), (1, 1, 0), (1, 0, 0)) + q((0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)) +
            q((0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1)) + q((0, 1, 0), (0, 1, 1), (1, 1, 1), (1, 1, 0)) +
            q((0, 0, 0), (0, 0, 1), (0, 1, 1), (0, 1, 0)) + q((1, 0, 0), (1, 1, 0), (1, 1, 1), (1, 0, 1)))


def world_tr(raw, pkg, e, depth=0):
    fl = T.fields(raw, pkg, e)
    g = lambda k: None if fl.get(k) in (None, "ZERO") else tuple(fl[k])
    tr = ut.Transform.from_relative(g("RelativeLocation"), g("RelativeRotation"), g("RelativeScale3D"))
    par = fl.get("AttachParent")
    if isinstance(par, tuple) and par[0] == "export" and 0 <= par[1] < len(pkg.exports) and depth < 12:
        return tr * world_tr(raw, pkg, pkg.exports[par[1]], depth + 1)
    return tr


def landscape_root(raw, pkg, comps):
    """(sum of RelativeLocation, RelativeScale3D) of the landscape root along the AttachParent
    chain — same as arcpak.mapglb.landscape_root."""
    par = comps[0]["parent"]
    loc, scale, seen = [0.0, 0.0, 0.0], [100.0, 100.0, 100.0], set()
    while True:
        if isinstance(par, tuple):
            par = par[1] if par[0] == "export" else None
        if not isinstance(par, int) or par in seen or not 0 <= par < len(pkg.exports):
            return tuple(loc), tuple(scale)
        seen.add(par)
        fl = T.fields(raw, pkg, pkg.exports[par])
        rl = fl.get("RelativeLocation") or (0.0, 0.0, 0.0)
        loc = [loc[k] + rl[k] for k in range(3)]
        if fl.get("RelativeScale3D"):
            scale = list(fl["RelativeScale3D"])
        par = fl.get("AttachParent")


def ref_export(pkg, v):
    r = game.ref(pkg, v)
    return r[1] if isinstance(r, tuple) and r[0] == "export" and 0 <= r[1] < len(pkg.exports) else None


def run(filter_sub, out=None, cell=100.0, strikt=False, hang=44.765, seed=None, dump=None):
    """Compute the play area of one map and write <out>. filter_sub: map sub-path, e.g.
    'Maps/PracticeRangeArray/'. Raises NoPlayArea when the map has no play area.
    Returns the output path."""
    global FILTER, NAME, CELL, STRIKT, HANG, SEED, DUMP, OUT
    FILTER = filter_sub
    NAME = FILTER.strip("/").split("/")[-1]
    CELL = float(cell)
    STRIKT = bool(strikt)
    HANG = None if hang in (None, "aus") else float(hang)
    SEED = seed
    DUMP = dump
    OUT = out or os.path.join(HERE, "out", "map", NAME + "_playarea.json")
    reset()
    return _run()


def _run():
    idx = iostore.path_index(game.C, exts=(".umap",))
    cells = sorted(p for p in idx if FILTER in p)
    vols, starts, limiters, terr = [], [], [], {}
    for n_zelle, p in enumerate(cells):
        report_progress(n_zelle, len(cells))
        c, ci = idx[p][0]
        raw = c.get_chunk(ci)
        pkg = zen.parse_uasset(raw, global_names=game.GN)
        cnt["Zellen"] += 1
        comps = mesh.components(raw, pkg, game.smap, game.read_unversioned, fields=T.fields)
        if comps and all(cp["heights"] for cp in comps):
            # Root per cell via the AttachParent chain (BuriedCity: 96 proxy cells, each with its own
            # root, scale (100,100,250)); key = world quad so that neighbouring cells line up.
            root, sc = landscape_root(raw, pkg, comps)
            ox, oy = round(root[0] / sc[0]), round(root[1] / sc[1])
            if terr.get("_s", sc[0]) != sc[0]:
                raise SystemExit(f"FEHLER: Landscape-Skala uneinheitlich {terr['_s']} / {sc[0]}")
            for cp in comps:
                rx, ry = cp["rel"]
                q = cp["q"]
                for y in range(q):
                    for x in range(q):
                        terr[(ox + rx + x, oy + ry + y)] = root[2] + (cp["heights"][y * q + x] - 32768) / 128.0 * sc[2]
            rest = (root[0] - ox * sc[0], root[1] - oy * sc[1], 0.0)   # root offset below one quad
            if "_root" in terr and max(abs(terr["_root"][k] - rest[k]) for k in (0, 1)) > 1e-3:
                raise SystemExit(f"FEHLER: Landscape-Wurzeln nicht im gemeinsamen Quad-Raster {terr['_root']} / {rest}")
            terr["_root"], terr["_s"] = rest, sc[0]
            cnt["Landscape-Zellen"] += 1
        elif comps:
            cnt["Landscape-Zellen ohne Höhen"] += 1
        for e in pkg.exports:
            cls = cls_of(pkg, e)
            if "PlayerStart" in cls:
                fl = dict(game.read_component(raw, pkg, e, cls, partial=True)[1])
                r = ref_export(pkg, fl.get("RootComponent"))
                if r is not None:
                    t = world_tr(raw, pkg, pkg.exports[r]).t
                    starts.append({"cell": p, "export": e.index, "class": cls, "loc": list(t)})
                    cnt["PlayerStart"] += 1
            elif cls == "PlayableAreaLimiter":
                fl = dict(game.read_component(raw, pkg, e, cls, partial=True)[1])
                r = ref_export(pkg, fl.get("Spline"))
                if r is None:
                    continue
                ok, sp, _ = game.read_component(raw, pkg, pkg.exports[r], cls_of(pkg, pkg.exports[r]))
                pos = dict(dict(sp["SplineCurves"])["position"])
                tr = world_tr(raw, pkg, pkg.exports[r])
                pts = [list(tr.apply(dict(q)["OutVal"])) for q in pos["Points"]]
                limiters.append({"cell": p, "export": e.index, "spline": r, "looped": bool(pos.get("bIsLooped")),
                                 "punkte": pts, "ok": ok,
                                 "MaxPlayableAreaDistanceDivergence": fl.get("MaxPlayableAreaDistanceDivergence")})
                cnt["PlayableAreaLimiter"] += 1
            elif cls == "BrushComponent":
                vol = pkg.exports[e.outer]
                vcls = cls_of(pkg, vol)
                fl = T.fields(raw, pkg, e)
                ok, pr, _ = game.read_component(raw, pkg, e, cls, partial=True)
                bsx = ref_export(pkg, dict(pr).get("BrushBodySetup"))
                if bsx is None:
                    cnt[f"Brush ohne BodySetup ({vcls})"] += 1
                    continue
                be = pkg.exports[bsx]
                a = pkg.header_size + be.offset
                cx = brush_convex(raw[a:a + be.size])
                if len(cx) != 1:
                    cnt[f"Brush-Konvexe ≠ 1 ({vcls}: {len(cx)})"] += 1
                    continue
                off, V, I = cx[0]
                cnt[f"Konvex bei Body-Offset {off}"] += 1
                if I is None:
                    I = box_tris(V)
                    cnt["IndexData fehlt → Quader trianguliert"] += 1
                if I is None:
                    cnt[f"keine Dreiecke ({vcls})"] += 1
                    continue
                tr = world_tr(raw, pkg, e)
                vols.append({"cell": p, "class": vcls, "export": vol.index, "brush": e.index,
                             "bodysetup": bsx, "matrix": [x for row in tr.matrix() for x in row],
                             "verts": [list(tr.apply(v)) for v in V], "tris": I})
                cnt[f"Volume {vcls}"] += 1
        T.forget(pkg)

    def ground(x, y):
        if "_root" not in terr:
            return None
        r, s = terr["_root"], terr["_s"]
        qx, qy = (x - r[0]) / s, (y - r[1]) / s
        x0, y0 = math.floor(qx), math.floor(qy)
        h = [terr.get((x0 + dx, y0 + dy)) for dy in (0, 1) for dx in (0, 1)]
        if None in h:
            return None
        fx, fy = qx - x0, qy - y0
        return (h[0] * (1 - fx) + h[1] * fx) * (1 - fy) + (h[2] * (1 - fx) + h[3] * fx) * fy

    # grid over the bbox of the BlockingVolumes
    bl = [v for v in vols if v["class"] == "BlockingVolume"]
    if not bl:
        for k in sorted(cnt):
            print(f"  {cnt[k]:10,}  {k}")
        kein_spielbereich("keine BlockingVolumes in den Zellen")
    nav = [v for v in vols if v["class"] == "NavMeshBoundsVolume"]
    allv = np.array([q for v in bl + nav for q in v["verts"]])
    cnt["Raster über BlockingVolumes" + (" ∪ NavMeshBoundsVolume" if nav else "")] += 1
    x0, y0 = allv[:, 0].min(), allv[:, 1].min()
    nx = int(math.ceil((allv[:, 0].max() - x0) / CELL)); ny = int(math.ceil((allv[:, 1].max() - y0) / CELL))
    xs = x0 + (np.arange(nx) + 0.5) * CELL; ys = y0 + (np.arange(ny) + 0.5) * CELL
    G = np.full((ny, nx), np.nan)
    for j, y in enumerate(ys):
        for i, x in enumerate(xs):
            g = ground(x, y)
            if g is not None:
                G[j, i] = g
    cnt["Raster-Zellen"] = nx * ny
    cnt["Raster-Zellen mit Terrain"] = int(np.isfinite(G).sum())
    X, Y = np.meshgrid(xs, ys)
    blocked = np.zeros((ny, nx), bool)
    for v in bl:
        M = np.array(v["matrix"]).reshape(4, 4)[:3]
        Minv = np.linalg.inv(M[:, :3])
        loc = [ut_v for ut_v in M[:, 3]]
        V = np.array(v["verts"])
        lo, hi = V.min(0), V.max(0)
        sel = (X >= lo[0]) & (X <= hi[0]) & (Y >= lo[1]) & (Y <= hi[1])
        if not sel.any():
            continue
        # local brush vertices (before the transform) → convex as intersection of triangle half-spaces
        Lv = (Minv @ (V - np.array(loc)).T).T
        tris = np.array(v["tris"]).reshape(-1, 3)
        nrm = np.cross(Lv[tris[:, 1]] - Lv[tris[:, 0]], Lv[tris[:, 2]] - Lv[tris[:, 0]])
        ctr = Lv.mean(0)
        d = np.einsum("ij,ij->i", nrm, Lv[tris[:, 0]])
        flip = np.einsum("ij,j->i", nrm, ctr) > d          # orient normals outward
        nrm[flip] *= -1; d[flip] *= -1
        hit = np.zeros(sel.sum(), bool)
        gz = G[sel]
        # Boundary wall: vertical slab, ≥ 100 m long and high. Some of these start above the
        # terrain (e.g. PracticeRangeArray west/east); the gap is closed by cliff meshes
        # (SM_South_Base_Cliff_Backdrop) whose collision is not read here → block such walls
        # over their full height in top view.
        dims = Lv.max(0) - Lv.min(0)                    # brush size per local axis
        axes = np.linalg.norm(M[:, :3] * dims, axis=0)  # world size per local axis
        thin = int(np.argmin(axes))
        vertical = abs(M[2, thin]) / (np.linalg.norm(M[:, thin]) or 1) < 0.1
        span_z = V[:, 2].max() - V[:, 2].min()
        span_xy = max(np.hypot(*(M[:2, k] * dims[k])) for k in range(3) if k != thin)
        if not STRIKT and vertical and span_z >= 10000 and span_xy >= 10000:
            v["grenzwand"] = True
            gz = np.full_like(gz, np.nan)          # footprint over its own Z span (below)
        for zoff in (50.0, 115.0, 180.0):
            Z = np.where(np.isfinite(gz), gz + zoff, np.nan)
            pts = np.stack([X[sel], Y[sel], Z], 1)
            Lp = (Minv @ (pts - np.array(loc)).T).T
            inside = np.all(Lp @ nrm.T <= d + 1e-6, axis=1) & np.isfinite(Z)
            hit |= inside
        # without terrain: the volume counts if it intersects the corridor [−2000, 8000]
        noterr = ~np.isfinite(gz)
        if noterr.any():
            zz = np.linspace(V[:, 2].min(), V[:, 2].max(), 201) if v.get("grenzwand") else np.linspace(-2000, 8000, 41)
            for z in zz:
                pts = np.stack([X[sel][noterr], Y[sel][noterr], np.full(noterr.sum(), z)], 1)
                Lp = (Minv @ (pts - np.array(loc)).T).T
                hh = np.all(Lp @ nrm.T <= d + 1e-6, axis=1)
                tmp = hit[noterr]; tmp |= hh; hit[noterr] = tmp
        sub = blocked[sel]; sub |= hit; blocked[sel] = sub
        v["sperrzellen"] = int(hit.sum())
    cnt["gesperrte Zellen"] = int(blocked.sum())

    seed = SEED or (tuple(starts[0]["loc"][:2]) if starts else None)
    if seed is None:
        kein_spielbereich("kein PlayerStart und kein --seed")
    si, sj = int((seed[0] - x0) // CELL), int((seed[1] - y0) // CELL)
    if not (0 <= si < nx and 0 <= sj < ny) or blocked[sj, si]:
        kein_spielbereich(f"Startzelle außerhalb/gesperrt: {seed}")
    reach = np.zeros((ny, nx), bool)
    reach[sj, si] = True
    stack = [(sj, si)]
    step = CELL * math.tan(math.radians(HANG)) + 45.0 if HANG is not None else None
    while stack:
        j, i = stack.pop()
        for dj, di in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            a, b = j + dj, i + di
            if 0 <= a < ny and 0 <= b < nx and not reach[a, b] and not blocked[a, b]:
                if step is not None and abs(G[a, b] - G[j, i]) > step:   # NaN comparison → False
                    cnt["Hangregel: Übergänge gesperrt"] += 1
                    continue
                reach[a, b] = True
                stack.append((a, b))
    cnt["erreichbare Zellen"] = int(reach.sum())
    dump = DUMP
    if dump:                                   # diagnostics: grid as npz
        np.savez_compressed(dump, reach=reach, blocked=blocked, G=G, x0=x0, y0=y0, cell=CELL)
    edge_touch = bool(reach[0, :].any() or reach[-1, :].any() or reach[:, 0].any() or reach[:, -1].any())
    cnt["Flutfüllung erreicht Rasterrand (Leck)" if edge_touch else "Flutfüllung geschlossen (kein Leck)"] += 1
    if edge_touch:
        # No closed frame of BlockingVolumes/slopes (BuriedCity: boundary via mesh collision, which
        # is not read here) → region = grid (with a NavMeshBoundsVolume: its box).
        print("WARNUNG: Spielbereich nicht geschlossen — Region ist nur eine Näherung "
              + ("(NavMeshBoundsVolume-Box)" if nav else "(Bbox der BlockingVolumes)"))

    # Outline: directed cell edges (reachable cell on the left), chained into loops
    R = np.pad(reach, 1)
    nxt = {}
    edges = []
    # Cell (j, i) of the padded grid covers corners x ∈ [i, i+1], y ∈ [j, j+1];
    # edges are directed so the reachable cell lies on the LEFT (outer boundary
    # counter-clockwise, area > 0; islands clockwise, area < 0)
    for j in range(R.shape[0] - 1):
        for i in range(R.shape[1] - 1):
            a, b = R[j, i], R[j, i + 1]       # vertical edge x = i+1, y j..j+1
            if a != b:
                edges.append(((i + 1, j), (i + 1, j + 1)) if a else ((i + 1, j + 1), (i + 1, j)))
            a, b = R[j, i], R[j + 1, i]       # horizontal edge y = j+1, x i..i+1
            if a != b:
                edges.append(((i + 1, j + 1), (i, j + 1)) if a else ((i, j + 1), (i + 1, j + 1)))
    for s, t in edges:
        nxt.setdefault(s, []).append(t)
    loops = []
    while nxt:
        s0 = next(iter(nxt))
        loop, s = [s0], s0
        while True:
            t = nxt[s].pop()
            if not nxt[s]:
                del nxt[s]
            if t == s0:
                break
            loop.append(t)
            s = t
        loops.append(loop)

    def dp(pts, eps):
        """Douglas-Peucker, iterative, open polyline (endpoints are kept)."""
        keep = [False] * len(pts)
        keep[0] = keep[-1] = True
        st = [(0, len(pts) - 1)]
        while st:
            i0, i1 = st.pop()
            (ax, ay), (bx, by) = pts[i0], pts[i1]
            L = math.hypot(bx - ax, by - ay) or 1e-9
            dmax, k = 0.0, -1
            for i in range(i0 + 1, i1):
                d = abs((bx - ax) * (pts[i][1] - ay) - (by - ay) * (pts[i][0] - ax)) / L
                if d > dmax:
                    dmax, k = d, i
            if k >= 0 and dmax > eps:
                keep[k] = True
                st += [(i0, k), (k, i1)]
        return [q for q, f in zip(pts, keep) if f]

    def dp_loop(W, eps):
        """Closed loop: split at the point farthest from the anchor and simplify both halves."""
        k = max(range(len(W)), key=lambda i: math.dist(W[0], W[i]))
        return dp(W[:k + 1], eps)[:-1] + dp(W[k:] + [W[0]], eps)[:-1]

    out_loops = []
    for lp in loops:
        W = [(x0 + (i - 1) * CELL, y0 + (j - 1) * CELL) for i, j in lp]
        area = 0.5 * sum(W[k - 1][0] * W[k][1] - W[k][0] * W[k - 1][1] for k in range(len(W)))
        per = sum(math.dist(W[k], W[k - 1]) for k in range(len(W)))
        k0 = max(range(len(W)), key=lambda k: (W[k][0], W[k][1]))   # corner as DP anchor
        W = W[k0:] + W[:k0]
        S = dp_loop(W, 75.0)
        pts = []
        for x, y in S:
            g = ground(x, y)
            pts.append([x, y, g])
        out_loops.append({"punkte": pts, "flaeche_m2": abs(area) / 1e4, "umfang_m": per / 100,
                          "art": "Außengrenze" if area > 0 else "Insel (gesperrt innen)"})
    out_loops.sort(key=lambda l: -l["flaeche_m2"])
    cnt["Umriss-Schleifen"] = len(out_loops)

    # Region = outer boundary with islands filled: everything that can NOT be reached from the
    # grid border via unreachable cells (used for cropping by `mapexport.py glb --crop`)
    outside = np.zeros((ny, nx), bool)
    st = [(j, i) for j in range(ny) for i in (0, nx - 1)] + [(j, i) for i in range(nx) for j in (0, ny - 1)]
    st = [(j, i) for j, i in st if not reach[j, i]]
    for j, i in st:
        outside[j, i] = True
    while st:
        j, i = st.pop()
        for dj, di in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            a, b = j + dj, i + di
            if 0 <= a < ny and 0 <= b < nx and not outside[a, b] and not reach[a, b]:
                outside[a, b] = True
                st.append((a, b))
    region = ~outside
    cnt["Region-Zellen (Inseln gefüllt)"] = int(region.sum())
    region_runs = []
    for j in range(ny):
        row = region[j]
        i = 0
        while i < nx:
            if row[i]:
                i0 = i
                while i < nx and row[i]:
                    i += 1
                region_runs.append([j, i0, i - i0])
            else:
                i += 1

    # Carpet: reachable cells per row as runs [j, i0, [ground height cm | None, …]]
    runs = []
    for j in range(ny):
        i = 0
        while i < nx:
            if not reach[j, i]:
                i += 1
                continue
            i0 = i
            while i < nx and reach[j, i]:
                i += 1
            runs.append([j, i0, [None if not np.isfinite(g) else round(float(g), 1) for g in G[j, i0:i]]])
    res = {"map": NAME, "zelle_cm": CELL, "teppich": runs, "region": region_runs, "seed": list(seed), "raster": [x0, y0, nx, ny],
           "flaeche_m2": float(reach.sum()) * CELL * CELL / 1e4, "geschlossen": not edge_touch, "loops": out_loops,
           "volumes": vols, "player_starts": starts, "limiter": limiters, "stats": dict(cnt)}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(res, open(OUT, "w"))
    for k in sorted(cnt):
        print(f"  {cnt[k]:10,}  {k}")
    print(f"Startpunkt {tuple(round(v) for v in seed)} | erreichbare Fläche {res['flaeche_m2']:,.0f} m²")
    for l in out_loops[:8]:
        P_ = np.array([q[:2] for q in l["punkte"]])
        print(f"  Schleife {l['art']:24} {len(l['punkte']):4} Punkte  {l['flaeche_m2']:10,.0f} m²  Umfang {l['umfang_m']:7,.0f} m  "
              f"X {P_[:,0].min():8.0f}..{P_[:,0].max():8.0f} Y {P_[:,1].min():8.0f}..{P_[:,1].max():8.0f}")
    for v in vols:
        if v["class"] == "BlockingVolume":
            print(f"  BlockingVolume {v['export']:4} sperrt {v.get('sperrzellen', 0):7,} Zellen" + ("  [Grenzwand, volle Höhe]" if v.get("grenzwand") else ""))
    for s in starts:
        print("  PlayerStart", s["class"], [round(x) for x in s["loc"]])
    for L in limiters:
        print("  PlayableAreaLimiter", L["export"], "looped", L["looped"], [[round(x) for x in q] for q in L["punkte"]])
    print("geschrieben:", OUT)
    return OUT


def parse_options(argv) -> dict:
    """Command line -> run() keyword arguments."""
    args = list(argv)
    positional = [a for a in args if not a.startswith("--")]
    if not positional:
        raise SystemExit("playarea braucht einen Map-Pfad, z. B. Maps/PracticeRangeArray/")

    def opt(name, default=None):
        return next((a[len(name) + 1:] for a in args if a.startswith(name + "=")), default)

    seed = opt("--seed")
    hang = opt("--hang", "44.765")
    return {"filter_sub": positional[0],
            "out": opt("--out") or None,
            "cell": float(opt("--cell", 100)),
            "strikt": "--strikt" in args,
            "hang": None if hang == "aus" else float(hang),
            "seed": tuple(map(float, seed.split(","))) if seed else None,
            "dump": opt("--dump") or None}


def main(argv=None):
    return run(**parse_options(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    main()
