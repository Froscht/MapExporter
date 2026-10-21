"""Geometry readers: cooked StaticMesh and SkeletalMesh render data, spline-mesh deformation, landscape.

The StaticMesh render-data layout follows CUE4Parse (Apache-2.0), see NOTICE.
"""
import math
import struct


# ---------- static mesh render data ----------
SEP = 0x100


class Walk:
    def __init__(self, d, p, end):
        self.d, self.p, self.end = d, p, end

    def u8(self):
        v = self.d[self.p]; self.p += 1; return v

    def i32(self):
        v = struct.unpack_from("<i", self.d, self.p)[0]; self.p += 4; return v

    def skip(self, n):
        self.p += n
        if self.p > self.end:
            raise ValueError(f"über das Ende hinaus ({self.p - self.end} B)")

    def strip(self):
        g, c = self.d[self.p], self.d[self.p + 1]; self.p += 2; return g, c

    def bulk_array(self):
        """(offset, element size, count) of a bulk array; leaves the cursor behind it."""
        size, count = self.i32(), self.i32()
        if size < 0 or count < 0:
            raise ValueError(f"bulk array size={size} count={count}")
        off = self.p
        self.skip(size * count)
        return off, size, count


def read_buffers(w, nsec):
    """SerializeBuffers -> dict of buffer locations (absolute offsets into w.d)."""
    b = {}
    sg, sc = w.strip()
    b["pos_stride"] = w.i32()
    b["num_verts"] = w.i32()
    b["pos"] = w.bulk_array()
    vg, _vc = w.strip()
    b["num_uv"] = w.i32()
    nv2 = w.i32()
    b["full_uv"] = w.i32()
    b["high_tan"] = w.i32()
    if nv2 != b["num_verts"]:
        raise ValueError(f"NumVertices Position {b['num_verts']} gegen VertexBuffer {nv2}")
    b["tan"] = b["uv"] = None
    if not vg & 2:
        b["tan"] = w.bulk_array()
        b["uv"] = w.bulk_array()
    cg, _cc = w.strip()
    w.i32()
    ncv = w.i32()
    b["color"] = w.bulk_array() if (not cg & 2 and ncv > 0) else None

    def index_buffer():
        is32 = w.i32()
        arr = w.bulk_array()
        w.i32()                                                 # bShouldExpandTo32Bit
        return is32, arr
    b["index"] = index_buffer()
    b["rev_index"] = index_buffer() if not sc & 4 else None
    b["depth_index"] = index_buffer()
    b["rev_depth_index"] = index_buffer() if not sc & 4 else None
    if not sg & 1:
        index_buffer()                                          # Wireframe
    b["arc_index"] = index_buffer()                             # GAME_ArcRaiders
    b["arc_vertcount"] = w.i32()
    if not sc & 8:
        w.skip(24)
        w.bulk_array()                                          # rayTracingGeometry

    def sampler():
        w.skip(4 * w.i32()); w.skip(4 * w.i32()); w.skip(4)
    for _ in range(nsec):
        sampler()
    sampler()
    return b


def walk_native(d, p, end):
    w = Walk(d, p, end)
    out = {"lods": [], "nanite": None, "proxy": [], "df": []}
    w.strip()
    if w.i32() != 1:
        raise ValueError("bCooked != 1")
    w.i32(); w.i32()
    w.skip(16)
    w.skip(4 * w.i32())
    nlod = w.i32()
    if not 0 <= nlod <= 16:
        raise ValueError(f"LOD-Zahl {nlod}")
    for _ in range(nlod):
        lg, _lc = w.strip()
        ns = w.i32()
        if not 0 <= ns <= 512:
            raise ValueError(f"Sektionen {ns}")
        secs = []
        for _s in range(ns):
            mat, first, ntri, vmin, vmax = struct.unpack_from("<5i", d, w.p)
            secs.append((mat, first, ntri, vmin, vmax))
            w.skip(48)
        w.skip(56 + 4)
        cooked_out, inlined = w.i32(), w.i32()
        if cooked_out not in (0, 1) or inlined not in (0, 1):
            raise ValueError(f"bool außerhalb 0/1: {cooked_out} {inlined}")
        lod = {"sections": secs, "kind": "leer", "buffers": None, "bulk": None}
        if not lg & 2 and not cooked_out:
            w.skip(4)
            if inlined:
                lod["kind"], lod["buffers"] = "inline", read_buffers(w, ns)
            else:
                lod["kind"], lod["bulk"] = "bulk", w.i32()
                w.skip(116)
            w.skip(16)
        out["lods"].append(lod)
    w.u8()
    ng, _nc = w.strip()
    if not ng & 2:
        w.i32()
        out["nanite"] = w.i32()
        w.skip(w.i32())                                         # RootData

        def arr(elem):
            n = w.i32()
            if not 0 <= n <= (end - w.p) // max(1, elem):
                raise ValueError(f"Nanite-Feld {n} x {elem}")
            w.skip(n * elem)
        for elem in (20, 224, 4, 2, 48, 4, 4):
            arr(elem)
        w.skip(28)
        arr(2)
        w.skip(32)
    if w.i32():
        pg, _pc = w.strip()
        w.i32()
        if not pg & 2:
            for _ in range(w.i32()):
                ob = w.i32()
                if ob:
                    w.skip(48 * w.i32())
                ort, inl = w.i32(), w.i32()
                if inl:
                    raise ValueError("Proxy-LOD inline")
                out["proxy"].append(w.i32())
                if ob:
                    w.skip(44)
                if ort:
                    w.skip(32)
    cg, cc = w.strip()
    if not cg & 2 and not cc & 2:
        for _ in range(nlod):
            if w.i32():
                w.skip(53)
                w.skip(61 * w.i32())

    def df5():
        w.skip(29 + 112)
        w.skip(w.i32())
        w.skip(6)
        out["df"].append(w.i32())
    dg, dc = w.strip()
    if not dg & 2 and not dc & 1:
        for _ in range(nlod):
            if w.i32():
                df5()
            for _ in range(w.i32()):
                df5()
    out["bounds"] = struct.unpack_from("<7d", d, w.p)           # Origin, BoxExtent, SphereRadius
    w.skip(56)
    w.u8()
    for _ in range(8):
        cooked = w.i32()
        w.skip(4)
        if not cooked:
            w.skip(w.i32() * 12)
    w.strip()
    if w.i32():
        for _ in range(5):
            w.bulk_array()
    if w.i32():
        raise ValueError("bHasSpeedTreeWind")
    out["num_materials"] = w.i32()
    w.skip(40 * out["num_materials"])
    out["end"] = w.p
    return out


# ---------- decoding ------------------------------------------------------------------------------
def _half(h):
    return struct.unpack("<e", struct.pack("<H", h))[0]


def positions(d, b):
    off, size, n = b["pos"]
    if size != 12:
        raise ValueError(f"Positionsstride {size} (nur 12 = float3 gemessen)")
    import numpy as np
    # float64 result: reading float32 raw data exactly, but later Python/numpy arithmetic
    # then runs in double precision like the former struct.unpack list of tuples did
    return np.frombuffer(d, dtype="<f4", count=3 * n, offset=off).reshape(n, 3).astype(np.float64)


def indices(d, ib):
    is32, (off, size, n) = ib
    import numpy as np
    raw = d[off:off + size * n]
    if is32:
        return np.frombuffer(raw, dtype="<u4", count=len(raw) // 4)
    return np.frombuffer(raw, dtype="<u2", count=len(raw) // 2)


def normals(d, b):
    """TangentZ per vertex (FPackedNormal: Data ^ 0x80808080, byte / 127.5 - 1)."""
    if b["tan"] is None:
        return None
    import numpy as np
    off, size, n = b["tan"]
    if b["high_tan"] and size == 16:
        # High-precision tangents: FPackedRGBA16N pair per vertex, TangentX 4×u16 then
        # TangentZ 4×u16; each u16 ^ 0x8000, value = v / 32767.5 - 1
        u = np.frombuffer(d, dtype="<u2", count=8 * n, offset=off).reshape(n, 8)[:, 4:7]
        return (u.astype(np.int64) ^ 0x8000) / 32767.5 - 1
    if b["high_tan"] or size != 8:
        raise ValueError(f"Tangenten high_tan={b['high_tan']} size={size} (nicht gemessen)")
    z = np.frombuffer(d, dtype="<u4", count=2 * n, offset=off)[1::2] ^ 0x80808080
    out = np.empty((n, 3), dtype=np.float64)
    out[:, 0] = (z & 0xFF) / 127.5 - 1
    out[:, 1] = ((z >> 8) & 0xFF) / 127.5 - 1
    out[:, 2] = ((z >> 16) & 0xFF) / 127.5 - 1
    return out


def uvs(d, b, channel=0):
    if b["uv"] is None:
        return None
    import numpy as np
    off, size, count = b["uv"]
    ntc = b["num_uv"]
    n = b["num_verts"]
    if b["full_uv"]:
        if n * ntc * 8 <= len(d) - off:
            a = np.frombuffer(d, dtype="<f4", count=2 * ntc * n, offset=off).reshape(n, ntc, 2)
            return np.ascontiguousarray(a[:, channel, :], dtype=np.float64)
        # short buffer: fall back to the per-row reader (raises exactly like before)
        return [struct.unpack_from("<2f", d, off + (i * ntc + channel) * 8) for i in range(n)]
    if n * ntc * 4 <= len(d) - off:
        u = np.frombuffer(d, dtype="<u2", count=2 * ntc * n, offset=off).reshape(n, ntc, 2)[:, channel, :]
        # view, not astype: the u16 bits are the half-float, like struct "<e"
        return u.copy().view(np.float16).astype(np.float64)
    return [(_half(u), _half(v)) for u, v in
            (struct.unpack_from("<2H", d, off + (i * ntc + channel) * 4) for i in range(n))]


# ---------- skeletal mesh render data ----------
# Native USkeletalMesh data after the property block (cooked, UE5 large-world coordinates):
#   FStripDataFlags, FBoxSphereBounds ImportedBounds (7 doubles),
#   TArray<FSkeletalMaterial> (44 bytes each: FPackageIndex material, FName slot name, ...),
#   FReferenceSkeleton (bones: FName + parent index; ref pose: FTransform as 10 doubles;
#   name -> index map), bool32 bCooked, then the LODs (FSkeletalMeshLODRenderData).
# Per LOD: FStripDataFlags, bool32 bIsLODCookedOut, bool32 bInlined, RequiredBones (int16[]),
# render sections, ActiveBoneIndices (int16[]), u32 buffer size, then (inlined) the buffers:
#   FStripDataFlags, FMultiSizeIndexContainer (u8 data type size + bulk array),
#   FPositionVertexBuffer (stride, count, bulk) and FStaticMeshVertexBuffer (same layout as
#   in a StaticMesh: tangents + UVs). Skin weights and the rest follow and are not read.
SKEL_MATERIAL_SIZE = 44
SKEL_SECTION_TAIL = 30          # MaxBoneInfluences, cloth asset index, clothing data, flags


def walk_skeletal(d, p, end, lods=1):
    """Read the native part of a cooked SkeletalMesh starting at p (after the UObject trailer).
    -> {"bounds", "materials": [(package index, slot name index)], "bones": [(name index, parent)],
        "pose": [(quat, translation, scale)] (bone space, relative to the parent),
        "lods": [{"sections": [(mat, first index, ntri, first vertex, last vertex)], "buffers"}]}
    Only the first `lods` LODs are decoded (the reader stops there)."""
    w = Walk(d, p, end)
    out = {"lods": []}
    w.strip()
    out["bounds"] = struct.unpack_from("<7d", d, w.p)
    w.skip(56)
    mats = []
    for _ in range(w.i32()):
        mi, ni = struct.unpack_from("<iI", d, w.p)
        mats.append((mi, ni))
        w.skip(SKEL_MATERIAL_SIZE)
    out["materials"] = mats
    bones = []
    for _ in range(w.i32()):
        ni, _num, parent = struct.unpack_from("<IIi", d, w.p)
        bones.append((ni, parent))
        w.skip(12)
    out["bones"] = bones
    pose = []
    for _ in range(w.i32()):                     # reference pose: FTransform (quat, translation, scale)
        v = struct.unpack_from("<10d", d, w.p)
        pose.append((v[0:4], v[4:7], v[7:10]))
        w.skip(80)
    out["pose"] = pose
    w.skip(12 * w.i32())                         # name -> index map
    if w.i32() != 1:
        raise ValueError("SkeletalMesh not cooked")
    nlod = w.i32()
    if not 0 < nlod <= 16:
        raise ValueError(f"LOD count {nlod}")
    for _ in range(min(lods, nlod)):
        w.strip()
        cooked_out, inlined = w.i32(), w.i32()
        w.skip(2 * w.i32())                      # RequiredBones
        secs, bonemaps = [], []
        for _s in range(w.i32()):
            w.strip()
            mat, first, ntri = struct.unpack_from("<HII", d, w.p)
            w.skip(10)
            w.skip(4 + 1 + 4 + 4)                # bRecomputeTangent, mask channel, cast shadow, ray tracing
            base_vertex = w.i32()
            for _c in range(w.i32()):            # cloth mapping LODs: FMeshToMeshVertData, 64 bytes
                w.skip(64 * w.i32())
            nb = w.i32()                         # BoneMap: section bone index -> skeleton bone
            bonemaps.append(list(struct.unpack_from(f"<{nb}H", d, w.p)))
            w.skip(2 * nb)
            nverts = w.i32()
            w.skip(SKEL_SECTION_TAIL)
            secs.append((mat, first, ntri, base_vertex, base_vertex + nverts - 1))
        w.skip(2 * w.i32())                      # ActiveBoneIndices
        w.i32()                                  # buffer size
        lod = {"sections": secs, "bonemaps": bonemaps, "buffers": None}
        if cooked_out:
            out["lods"].append(lod)
            continue
        if not inlined:
            raise ValueError("SkeletalMesh LOD stored outside the package (not supported)")
        w.strip()
        b = {}
        dts = w.u8()
        b["index"] = (dts == 4, w.bulk_array())
        b["pos_stride"] = w.i32()
        b["num_verts"] = w.i32()
        b["pos"] = w.bulk_array()
        w.strip()
        b["num_uv"] = w.i32()
        if w.i32() != b["num_verts"]:
            raise ValueError("vertex count mismatch between position and vertex buffer")
        b["full_uv"] = w.i32()
        b["high_tan"] = w.i32()
        b["tan"] = w.bulk_array()
        b["uv"] = w.bulk_array()
        # FSkinWeightVertexBuffer: per vertex N bone indices (into the section BoneMap) then N weights
        w.strip()
        variable = w.i32()
        b["max_influences"] = w.i32()
        w.i32()                                  # total influence count
        if w.i32() != b["num_verts"]:
            raise ValueError("vertex count mismatch in the skin weight buffer")
        b["bone16"], b["weight16"] = w.i32(), w.i32()
        b["weights"] = w.bulk_array()
        b["weight_lookup"] = None
        if variable:                             # FSkinWeightLookupVertexBuffer: u32 (offset << 8 | count)
            w.strip()
            w.i32()
            b["weight_lookup"] = w.bulk_array()
        lod["buffers"] = b
        out["lods"].append(lod)
    return out


def skin_weights(d, b, sections, bonemaps, max_out=4):
    """Per vertex: ([skeleton bone] * max_out, [weight] * max_out), strongest influences first,
    weights normalized to 1. Section bone indices are mapped through the section BoneMap."""
    off, _size, _n = b["weights"]
    k = b["max_influences"]
    nv = b["num_verts"]
    raw = d[off:off + 2 * k * nv]
    joints, weights = [None] * nv, [None] * nv
    for (_mat, _first, _ntri, v0, v1), bmap in zip(sections, bonemaps):
        for v in range(v0, min(v1, nv - 1) + 1):
            rec = raw[2 * k * v:2 * k * (v + 1)]
            pairs = sorted(((rec[k + i], rec[i]) for i in range(k) if rec[k + i]), reverse=True)[:max_out]
            total = sum(w for w, _i in pairs) or 1
            jb = [bmap[i] if i < len(bmap) else 0 for _w, i in pairs]
            wb = [w / total for w, _i in pairs]
            jb += [0] * (max_out - len(jb))
            wb += [0.0] * (max_out - len(wb))
            if not pairs:
                wb[0] = 1.0
            joints[v], weights[v] = jb, wb
    for v in range(nv):                          # vertices outside every section (should not happen)
        if joints[v] is None:
            joints[v], weights[v] = [0] * max_out, [1.0] + [0.0] * (max_out - 1)
    return joints, weights


# ---------- spline mesh deformation ----------
DEFAULTS = {"StartPos": (0.0, 0.0, 0.0), "StartTangent": (100.0, 0.0, 0.0),
            "StartScale": (1.0, 1.0), "StartRoll": 0.0, "StartOffset": (0.0, 0.0),
            "EndPos": (100.0, 0.0, 0.0), "EndTangent": (100.0, 0.0, 0.0),
            "EndScale": (1.0, 1.0), "EndRoll": 0.0, "EndOffset": (0.0, 0.0)}

# ESplineMeshAxis: X=0, Y=1, Z=2. Axis permutation putting the forward axis first:
# (forward, local "x" cross axis, local "y" cross axis), as in UE CalcSliceTransform
# (X: Dir,XVec,YVec | Y: YVec,Dir,XVec | Z: XVec,YVec,Dir).
AXES = {0: (0, 1, 2), 1: (1, 2, 0), 2: (2, 0, 1)}


def params(fields):
    """Component fields (Templates.fields) -> complete parameter set with CDO defaults."""
    sp = dict(fields.get("SplineParams") or [])
    out = {}
    for k, v in DEFAULTS.items():
        x = sp.get(k, v)
        if x == "ZERO":                           # unversioned: zero fragment
            x = tuple(0.0 for _ in v) if isinstance(v, tuple) else 0.0
        out[k] = tuple(x) if isinstance(x, (list, tuple)) else x
    fa = fields.get("ForwardAxis")
    if isinstance(fa, str):                       # "enum:ESplineMeshAxis:N"
        fa = int(fa.rsplit(":", 1)[1])
    out["ForwardAxis"] = fa or 0
    out["SplineUpDir"] = tuple(fields.get("SplineUpDir") or (0.0, 0.0, 1.0))
    out["SplineBoundaryMin"] = fields.get("SplineBoundaryMin") or 0.0
    out["SplineBoundaryMax"] = fields.get("SplineBoundaryMax") or 0.0
    out["bSmoothInterpRollScale"] = bool(fields.get("bSmoothInterpRollScale"))
    return out


def _pos_coeffs(t):
    """Hermite weights of the position at t (t: float or numpy array)."""
    t2, t3 = t * t, t * t * t
    return 2 * t3 - 3 * t2 + 1, t3 - 2 * t2 + t, t3 - t2, -2 * t3 + 3 * t2


def _dir_coeffs(t):
    """Hermite weights of the tangent direction at t (t: float or numpy array)."""
    t2 = t * t
    return 6 * t2 - 6 * t, 3 * t2 - 4 * t + 1, 3 * t2 - 2 * t, -6 * t2 + 6 * t


def _norm(a):
    L = math.sqrt(a[0] ** 2 + a[1] ** 2 + a[2] ** 2)
    return (a[0] / L, a[1] / L, a[2] / L) if L > 1e-12 else (0.0, 0.0, 0.0)


def _norm_rows(A):
    """_norm for an (m, 3) array — same operations, same evaluation order."""
    import numpy as np
    L2 = A[:, 0] * A[:, 0] + A[:, 1] * A[:, 1] + A[:, 2] * A[:, 2]
    L = np.sqrt(L2)
    ok = L > 1e-12
    return np.where(ok[:, None], A / np.where(ok, L, 1.0)[:, None], 0.0)


def eval_pos(p, t):
    a, b, c, d = _pos_coeffs(t)
    S, ST, ET, E = p["StartPos"], p["StartTangent"], p["EndTangent"], p["EndPos"]
    return tuple(a * S[i] + b * ST[i] + c * ET[i] + d * E[i] for i in range(3))


def eval_dir(p, t):
    a, b, c, d = _dir_coeffs(t)
    S, ST, ET, E = p["StartPos"], p["StartTangent"], p["EndTangent"], p["EndPos"]
    return _norm(tuple(a * S[i] + b * ST[i] + c * ET[i] + d * E[i] for i in range(3)))


def _frames(p, t):
    """Vectorized CalcSliceTransform for the parameter list t: arrays
    (pos incl. offset, Dir, XVec, YVec, scale_x, scale_y) with one row per t."""
    import numpy as np
    t = np.asarray(t, dtype=np.float64)
    S, ST, ET, E = (np.asarray(p[k], dtype=np.float64)
                    for k in ("StartPos", "StartTangent", "EndTangent", "EndPos"))
    a, b, c, d = (w[:, None] for w in _pos_coeffs(t))
    pos = a * S + b * ST + c * ET + d * E
    a, b, c, d = (w[:, None] for w in _dir_coeffs(t))
    dirs = _norm_rows(a * S + b * ST + c * ET + d * E)
    up = np.asarray(p["SplineUpDir"], dtype=np.float64)
    bx = _norm_rows(np.stack([up[1] * dirs[:, 2] - up[2] * dirs[:, 1],
                              up[2] * dirs[:, 0] - up[0] * dirs[:, 2],
                              up[0] * dirs[:, 1] - up[1] * dirs[:, 0]], axis=1))
    by = _norm_rows(np.stack([dirs[:, 1] * bx[:, 2] - dirs[:, 2] * bx[:, 1],
                              dirs[:, 2] * bx[:, 0] - dirs[:, 0] * bx[:, 2],
                              dirs[:, 0] * bx[:, 1] - dirs[:, 1] * bx[:, 0]], axis=1))
    h = t * t * (3 - 2 * t) if p["bSmoothInterpRollScale"] else t
    so, eo = np.asarray(p["StartOffset"], np.float64), np.asarray(p["EndOffset"], np.float64)
    pos = pos + bx * (so[0] + (eo[0] - so[0]) * h)[:, None]
    pos = pos + by * (so[1] + (eo[1] - so[1]) * h)[:, None]
    ss, es = np.asarray(p["StartScale"], np.float64), np.asarray(p["EndScale"], np.float64)
    sc0 = ss[0] + (es[0] - ss[0]) * h
    sc1 = ss[1] + (es[1] - ss[1]) * h
    roll = p["StartRoll"] + (p["EndRoll"] - p["StartRoll"]) * h
    # math.cos/sin per value (np.cos/np.sin could differ by an ULP → different GLB bytes)
    cs = np.array([math.cos(r) for r in roll], dtype=np.float64)
    sn = np.array([math.sin(r) for r in roll], dtype=np.float64)
    xv = cs[:, None] * bx - sn[:, None] * by
    yv = cs[:, None] * by + sn[:, None] * bx
    return pos, dirs, xv, yv, sc0, sc1


def slice_frame(p, t):
    """(pos incl. offset, Dir, XVec, YVec, scale) at parameter t, as UE CalcSliceTransform."""
    o, d, xv, yv, s0, s1 = _frames(p, (float(t),))
    return tuple(o[0]), tuple(d[0]), tuple(xv[0]), tuple(yv[0]), (float(s0[0]), float(s1[0]))


def deform(pos, nrm, p, mesh_min, mesh_max):
    """Bend positions/normals in component space. mesh_min/max: mesh bounds (3-tuples);
    like UE, the StaticMesh bounds along the forward axis are used unless
    SplineBoundaryMin/Max are set."""
    import numpy as np
    f, ax, ay = AXES[p["ForwardAxis"]]
    lo, hi = mesh_min[f], mesh_max[f]
    if p["SplineBoundaryMin"] < p["SplineBoundaryMax"]:
        lo, hi = p["SplineBoundaryMin"], p["SplineBoundaryMax"]
    L = hi - lo if hi > lo else 1.0
    P = np.asarray(pos, dtype=np.float64)
    if P.size == 0:
        e = np.empty((0, 3), dtype=np.float64)
        return e, (e.copy() if nrm is not None else None)
    o, d, xv, yv, sc0, sc1 = _frames(p, (P[:, f] - lo) / L)
    out_p = (o + xv * (P[:, ax] * sc0)[:, None]) + yv * (P[:, ay] * sc1)[:, None]
    if nrm is None:
        return out_p, None
    N = np.asarray(nrm, dtype=np.float64)
    ok0, ok1 = np.abs(sc0) > 1e-9, np.abs(sc1) > 1e-9
    m = d * N[:, f][:, None]
    m = m + xv * (N[:, ax] / np.where(ok0, sc0, 1.0))[:, None]
    m = m + yv * (N[:, ay] / np.where(ok1, sc1, 1.0))[:, None]
    return out_p, _norm_rows(m)


def _selftest():
    # straight X spline with CDO defaults: (0..100) -> identity
    p = params({})
    q, _ = deform([(0, 5, 7), (100, -3, 2), (50, 1, 1)], None, p, (0, -10, -10), (100, 10, 10))
    assert all(abs(a - b) < 1e-9 for u, w in zip(q, [(0, 5, 7), (100, -3, 2), (50, 1, 1)])
               for a, b in zip(u, w)), q
    # 90° spline towards +Y: local +Y (cross axis) must map to -X (Up × Dir = Z × Y = -X)
    p = params({"SplineParams": [("EndPos", (0.0, 100.0, 0.0)), ("StartTangent", (0.0, 100.0, 0.0)),
                                 ("EndTangent", (0.0, 100.0, 0.0))]})
    q, _ = deform([(100, 1, 0)], None, p, (0, -1, -1), (100, 1, 1))
    assert all(abs(a - b) < 1e-9 for a, b in zip(q[0], (-1, 100, 0))), q


_selftest()


# ---------- landscape heightfield ----------
Q = 64          # default vertices per edge (NumSubsections=1, ComponentSizeQuads=63).
#                 Larger components exist (e.g. ComponentSizeQuads=126 -> 127 per edge); the
#                 actual Q is derived per component from the height count N = Q² (key "q").
S = 100.0       # cm per quad (RelativeScale3D X/Y of the landscape root)
ZS = 100.0      # cm per height unit (RelativeScale3D Z of the landscape root)


def parse_heights(tail):
    """Deterministic parser: N×uint16 heights (N = Q², Q vertices per edge) or None. No
    heuristics: the structure fields (1, 0, N, n, N*(n+2)) must match exactly and N must
    be a perfect square (e.g. N=4096=64², N=16129=127²)."""
    if len(tail) < 36:
        return None
    if struct.unpack_from("<i", tail, 0)[0] != 1:
        return None
    if struct.unpack_from("<i", tail, 20)[0] != 0:
        return None
    N = struct.unpack_from("<i", tail, 24)[0]
    q = int(round(N ** 0.5)) if N > 0 else 0
    if q < 2 or q * q != N:
        return None
    n = struct.unpack_from("<i", tail, 28)[0]
    if not 0 <= n <= 64:
        return None
    if len(tail) < 36 + 8 * n + 2 * N:
        return None
    if struct.unpack_from("<i", tail, 32 + 8 * n)[0] != N * (n + 2):
        return None
    return struct.unpack_from(f"<{N}H", tail, 36 + 8 * n)


def q_of(heights):
    return int(round(len(heights) ** 0.5)) if heights else None


def local_box(body, quads=63):
    """CachedLocalBox, found as the FBox pattern Min=(0,0,z1) Max=(quads,quads,z2) (doubles)."""
    for off in range(0, len(body) - 56):
        m = struct.unpack_from("<3d", body, off)
        M = struct.unpack_from("<3d", body, off + 24)
        if m[0] == 0.0 and m[1] == 0.0 and M[0] == quads and M[1] == quads and abs(M[2]) < 1e4:
            return m[2], M[2]
    return None


def components(raw, pkg, smap, read_unversioned, fields=None):
    """All LandscapeComponents of the cell:
    [{index, rel (quad grid), heights, q, box, parent}].
    fields: optional (raw, pkg, e) -> fields incl. template chain (as in arcpak.placements);
    RelativeLocation is absent on the origin component (default 0)."""
    out = []
    for e in pkg.exports:
        cls = pkg.export_class(e, script_names=smap)
        if cls != "LandscapeComponent":
            continue
        a = pkg.header_size + e.offset
        _q, pr = read_unversioned(raw, a, a + e.size, cls)
        d = dict(pr)
        # RelativeLocation: direct values first (for LandscapeComponents the property reader
        # does not end exactly; the height trailer is not part of the properties).
        # AttachParent: template chain first. The direct reader can return the root of an
        # unrelated actor (e.g. a MergedScatterActor via the Outer chain); the template chain
        # yields the landscape actor's RootComponent.
        fl = fields(raw, pkg, e) if fields else d
        rl = d.get("RelativeLocation") or fl.get("RelativeLocation") or (0.0, 0.0, 0.0)
        h = parse_heights(raw[_q:a + e.size])
        q = q_of(h) or Q
        out.append({"index": e.index, "rel": (int(round(rl[0])), int(round(rl[1]))),
                    "heights": h, "q": q,
                    "box": local_box(raw[a:a + e.size], q - 1),
                    "parent": fl.get("AttachParent") or d.get("AttachParent")})
    return out


def continuity(comps):
    """(identical edges, total edges, summed |Δ|) between neighbouring components."""
    ok = tot = dsum = 0
    by = {c["rel"]: c for c in comps if c["heights"]}
    for (rx, ry), c in by.items():
        Q = c["q"]
        for dx, dy in ((1, 0), (0, 1)):
            n = by.get((rx + (Q - 1) * dx, ry + (Q - 1) * dy))
            if n is None or n["q"] != Q:
                continue
            if dx:
                ea = [c["heights"][t * Q + Q - 1] for t in range(Q)]
                eb = [n["heights"][t * Q] for t in range(Q)]
            else:
                ea = [c["heights"][(Q - 1) * Q + t] for t in range(Q)]
                eb = [n["heights"][t] for t in range(Q)]
            tot += 1
            dsum += sum(abs(a - b) for a, b in zip(ea, eb))
            ok += ea == eb
    return ok, tot, dsum


def build(comps, root=(0.0, 0.0, 0.0), s=S, zs=ZS):
    """Welded terrain grid in cell-local UE cm (left-handed, Z up).
    Welding: shared quad positions of neighbouring components become one vertex (their
    border heights are byte-identical). Normals are accumulated from face normals.
    Returns (pos, idx, nrm, stats); winding (v00,v10,v11),(v00,v11,v01) faces +Z."""
    pos, g, welded = [], {}, 0
    for cp in comps:
        h = cp["heights"]
        if h is None:
            continue
        rx, ry = cp["rel"]
        Q = cp["q"]
        for y in range(Q):
            row = y * Q
            for x in range(Q):
                key = (rx + x, ry + y)
                if key in g:
                    welded += 1
                    continue
                g[key] = len(pos)
                pos.append((root[0] + (rx + x) * s,
                            root[1] + (ry + y) * s,
                            root[2] + (h[row + x] - 32768) / 128.0 * zs))
    idx = []
    for cp in comps:
        if cp["heights"] is None:
            continue
        rx, ry = cp["rel"]
        Q = cp["q"]
        for y in range(Q - 1):
            for x in range(Q - 1):
                a = g[(rx + x, ry + y)]
                b = g[(rx + x + 1, ry + y)]
                c = g[(rx + x + 1, ry + y + 1)]
                d = g[(rx + x, ry + y + 1)]
                idx += [a, b, c, a, c, d]
    nrm = [[0.0, 0.0, 0.0] for _ in pos]
    for i in range(0, len(idx), 3):
        a, b, c = pos[idx[i]], pos[idx[i + 1]], pos[idx[i + 2]]
        ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        for j in (idx[i], idx[i + 1], idx[i + 2]):
            nrm[j][0] += nx
            nrm[j][1] += ny
            nrm[j][2] += nz
    up = 0
    for v in nrm:
        L = (v[0] ** 2 + v[1] ** 2 + v[2] ** 2) ** 0.5
        if L > 1e-9:
            v[0] /= L
            v[1] /= L
            v[2] /= L
        up += v[2] > 0
    stats = {"vertices": len(pos), "dreiecke": len(idx) // 3,
             "geschweißt": welded, "normale_+z": up}
    return pos, idx, [tuple(v) for v in nrm], stats
