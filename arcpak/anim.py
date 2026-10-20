"""Animations: decode cooked AnimSequences and find the clips of a skeleton.

Supported: the engine codec "AnimCompress_BitwiseCompressOnly" with constant key spacing
(ConstantKeyLerp) and float formats (translation Float96, rotation Float96NoW, scale Float96),
which is what nearly all ARC enemy clips use. ACL-compressed and per-track-compressed clips are
reported as unsupported.

Native AnimSequence data after the properties (cooked):
    FGuid, FStripDataFlags, bool32 bCooked, i32 CompressedRawDataSize,
    TArray<i32> track -> skeleton bone, i32, i32 buffer size, [padding], buffer,
    FString bone codec, FString curve codec, i32 curve bytes + data,
    i32 NumberOfKeys, u8 key format, u8 translation / rotation / scale format,
    i32 byte stream size, i32 track offset count, i32 scale offset count, i32 scale strip size.
The buffer holds the track offsets (per track: translation offset, keys, rotation offset, keys),
the scale offsets (per track: offset, keys) and the key byte stream.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

from . import game

_anim_index = None                      # skeleton package (lower case) -> [anim packages]


@dataclass
class Clip:
    name: str
    length: float
    num_keys: int
    tracks: dict = field(default_factory=dict)   # bone name -> (translations, rotations, scales)

    def times(self, n):
        if n <= 1 or self.num_keys <= 1:
            return [0.0]
        step = self.length / (self.num_keys - 1)
        return [i * step for i in range(n)]


def skeleton_bones(skel_gpath: str) -> list[str]:
    """Bone names of a Skeleton asset (its reference skeleton, in skeleton bone order)."""
    from . import models
    raw, pkg = models.package(skel_gpath)
    for e in pkg.exports:
        if pkg.export_class(e, script_names=game.smap) != "Skeleton":
            continue
        a = pkg.header_size + e.offset
        q, props = game.read_unversioned(raw, a, a + e.size, "Skeleton")
        q = game.read_native_tail(raw, q, a + e.size, "Skeleton", props, False)
        n, = struct.unpack_from("<i", raw, q)
        names = []
        for i in range(n):
            ni, = struct.unpack_from("<I", raw, q + 4 + 12 * i)
            names.append(pkg.names[ni] if ni < len(pkg.names) else f"bone{i}")
        return names
    raise ValueError(f"{skel_gpath}: no Skeleton export")


def _anim_export(raw, pkg):
    for e in pkg.exports:
        if pkg.export_class(e, script_names=game.smap) == "AnimSequence":
            return e
    return None


def anims_for(skel_gpath: str) -> list[str]:
    """AnimSequence packages that use this skeleton (scans the enemy, item and character
    folders once)."""
    global _anim_index
    from . import models
    if _anim_index is None:
        _anim_index = {}
        for k in models.index():
            if not k.endswith(".uasset") or not any(f in k for f in ("/Enemies/", "/Items/", "/Characters/Backpacks/")):
                continue
            stem = k.rsplit("/", 1)[1]
            if not stem.startswith(("A_", "AS_", "Anim_")):
                continue
            g = models.to_gpath(k)
            try:
                raw, pkg = models.package(g)
            except Exception:
                continue
            if _anim_export(raw, pkg) is None:
                continue
            for imp in pkg.imported_package_names() or []:
                if imp.endswith("_Skeleton") or "Skeleton" in imp.rsplit("/", 1)[-1]:
                    _anim_index.setdefault(imp.lower(), []).append(g)
    return sorted(_anim_index.get(skel_gpath.lower(), []), key=lambda g: g.rsplit("/", 1)[-1].lower())


def load_clip(gpath: str, bone_names: list[str]) -> Clip:
    """Decode an AnimSequence; bone_names = bone names of its Skeleton asset."""
    from . import models
    raw, pkg = models.package(gpath)
    e = _anim_export(raw, pkg)
    if e is None:
        raise ValueError("no AnimSequence")
    a = pkg.header_size + e.offset
    q, props = game.read_unversioned(raw, a, a + e.size, "AnimSequence")
    props = dict(props)
    if str(props.get("AdditiveAnimType", "")).endswith(("1", "2")):
        raise ValueError("additive animation (not supported)")
    q = game.read_native_tail(raw, q, a + e.size, "AnimSequence", props, bool(e.object_flags & 0x10))
    d = raw[q:a + e.size]
    p = 16 + 2 + 4                              # FGuid, strip flags, bCooked
    _raw_size, ntr = struct.unpack_from("<ii", d, p)
    p += 8
    track_bones = struct.unpack_from(f"<{ntr}i", d, p)
    p += 4 * ntr
    _x, size = struct.unpack_from("<ii", d, p)
    p += 8
    for pad in range(0, 64, 4):                 # the buffer ends right before the codec name
        n, = struct.unpack_from("<i", d, p + pad + size)
        if 0 < n < 200 and d[p + pad + size + 4:p + pad + size + 3 + n].isascii():
            break
    else:
        raise ValueError("codec name not found")
    buf = p + pad
    c = buf + size
    n, = struct.unpack_from("<i", d, c)
    codec = d[c + 4:c + 3 + n].decode("ascii", "replace")
    c += 4 + n
    n, = struct.unpack_from("<i", d, c)
    c += 4 + n
    n, = struct.unpack_from("<i", d, c)
    c += 4 + n
    if not codec.startswith("AnimCompress_BitwiseCompressOnly"):
        raise ValueError(f"codec {codec.rsplit('_', 1)[0]} not supported")
    num_keys, = struct.unpack_from("<i", d, c)
    key_fmt, t_fmt, r_fmt, s_fmt = d[c + 4:c + 8]
    stream_size, n_off, n_scale, _strip = struct.unpack_from("<iiii", d, c + 8)
    if key_fmt != 0 or t_fmt != 0 or r_fmt != 1 or s_fmt != 0:
        raise ValueError(f"key format {key_fmt}/{t_fmt}/{r_fmt}/{s_fmt} not supported")
    if 4 * n_off + 4 * n_scale + stream_size != size:
        raise ValueError("buffer layout mismatch")
    offsets = struct.unpack_from(f"<{n_off}i", d, buf)
    scale_offsets = struct.unpack_from(f"<{n_scale}i", d, buf + 4 * n_off)
    bs = buf + 4 * n_off + 4 * n_scale

    def vec3(off, keys):
        if off < 0 or keys <= 0:
            return None
        return [struct.unpack_from("<3f", d, bs + off + 12 * k) for k in range(keys)]

    clip = Clip(gpath.rsplit("/", 1)[-1], float(props.get("SequenceLength") or 0.0), num_keys)
    for t, bone in enumerate(track_bones):
        if not 0 <= bone < len(bone_names) or 4 * t + 3 >= len(offsets):
            continue
        to, tn, ro, rn = offsets[4 * t:4 * t + 4]
        trans = vec3(to, tn)
        rots = None
        xyz = vec3(ro, rn)
        if xyz:
            rots = [(x, y, z, math.sqrt(max(0.0, 1.0 - x * x - y * y - z * z))) for x, y, z in xyz]
        scales = None
        if n_scale and 2 * t + 1 < len(scale_offsets):
            scales = vec3(*scale_offsets[2 * t:2 * t + 2])
        clip.tracks[bone_names[bone]] = (trans, rots, scales)
    return clip


# ---------- pose evaluation (preview) ----------
def _trs_matrices(q, t, s):
    """4x4 matrices (UE space) from arrays of quaternions (n,4 xyzw), translations, scales."""
    import numpy as np
    x, y, z, w = q[:, 0], q[:, 1], q[:, 2], q[:, 3]
    M = np.zeros((len(q), 4, 4))
    M[:, 0, 0] = 1 - 2 * (y * y + z * z); M[:, 0, 1] = 2 * (x * y - z * w); M[:, 0, 2] = 2 * (x * z + y * w)
    M[:, 1, 0] = 2 * (x * y + z * w); M[:, 1, 1] = 1 - 2 * (x * x + z * z); M[:, 1, 2] = 2 * (y * z - x * w)
    M[:, 2, 0] = 2 * (x * z - y * w); M[:, 2, 1] = 2 * (y * z + x * w); M[:, 2, 2] = 1 - 2 * (x * x + y * y)
    M[:, :3, :3] *= s[:, None, :]
    M[:, :3, 3] = t
    M[:, 3, 3] = 1.0
    return M


class Poser:
    """CPU skinning of models.MeshData lists: positions for a clip at a given time.
    Uses the same merged skeleton as the GLB export (bones joined by name)."""

    def __init__(self, meshes):
        import numpy as np
        from .models import merged_skeleton
        skel = merged_skeleton(meshes)
        if not skel:
            raise ValueError("no skeleton")
        self.names, self.parents, pose = skel
        self.index = {n: i for i, n in enumerate(self.names)}
        self.q0 = np.array([p[0] for p in pose], float)
        self.t0 = np.array([p[1] for p in pose], float)
        self.s0 = np.array([p[2] for p in pose], float)
        self.inv_bind = np.linalg.inv(self._world(_trs_matrices(self.q0, self.t0, self.s0)))
        self.parts = []                          # (positions (n,4), joints (n,4), weights (n,4))
        for m in meshes:
            P = np.c_[np.asarray(m.pos, float), np.ones(len(m.pos))]
            if m.joints is not None:
                remap = np.array([self.index[n] for n in m.bone_names])
                J = remap[np.minimum(np.asarray(m.joints), len(remap) - 1)]
                W = np.asarray(m.weights, float)
            else:
                J = np.full((len(m.pos), 4), self.index.get(m.rigid_bone, 0))
                W = np.zeros((len(m.pos), 4)); W[:, 0] = 1.0
            self.parts.append((P, J, W))

    def _world(self, local):
        world = local.copy()
        for i, p in enumerate(self.parents):     # parents always come before their children
            if p >= 0:
                world[i] = world[p] @ local[i]
        return world

    @staticmethod
    def _sample(keys, f):
        import numpy as np
        k = np.asarray(keys, float)
        if len(k) == 1:
            return k[0]
        f = min(max(f, 0.0), len(k) - 1.0)
        i = int(f)
        j = min(i + 1, len(k) - 1)
        a = f - i
        return k[i], k[j], a

    def evaluate(self, clip, t):
        """[positions (n,3) in UE space] per mesh for clip at time t (seconds)."""
        import numpy as np
        q, tr, sc = self.q0.copy(), self.t0.copy(), self.s0.copy()
        n = max(clip.num_keys - 1, 1)
        f = (t / clip.length * n) if clip.length > 0 else 0.0
        for bone, (trans, rots, scales) in clip.tracks.items():
            b = self.index.get(bone)
            if b is None:
                continue
            for keys, target, is_rot in ((trans, tr, False), (rots, q, True), (scales, sc, False)):
                if not keys:
                    continue
                s = self._sample(keys, f * (len(keys) - 1) / n if len(keys) > 1 else 0.0)
                if isinstance(s, tuple):
                    k0, k1, a = s
                    if is_rot:
                        if np.dot(k0, k1) < 0:
                            k1 = -k1
                        v = k0 * (1 - a) + k1 * a
                        v = v / np.linalg.norm(v)
                    else:
                        v = k0 * (1 - a) + k1 * a
                else:
                    v = s
                target[b] = v
        skin = self._world(_trs_matrices(q, tr, sc)) @ self.inv_bind
        out = []
        for P, J, W in self.parts:
            acc = np.zeros((len(P), 4))
            for k in range(4):
                w = W[:, k]
                sel = w > 0
                if sel.any():
                    acc[sel] += w[sel, None] * np.einsum("nij,nj->ni", skin[J[sel, k]], P[sel])
            out.append(acc[:, :3])
        return out
