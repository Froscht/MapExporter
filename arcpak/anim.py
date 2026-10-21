"""Animations: decode cooked AnimSequences and find the clips of a skeleton.

Supported: the engine codec "AnimCompress_BitwiseCompressOnly" with constant key spacing
(ConstantKeyLerp) and float formats (translation Float96, rotation Float96NoW, scale Float96),
the codec "AnimCompress_PerTrackCompression" (per-track regions, Float96NoW / Fixed48NoW /
IntervalFixed32NoW / Fixed32NoW / Identity keys, stripped keys with a key->frame table), and
the ACL codec "AnimBoneCompressionCodec_ACL*" (compressed_tracks v02_01_00, uniform
sampling, drop-W variable bit rate rotations, variable bit rate vectors). Database/optimized
ACL codecs are reported as unsupported. Local-space additive clips (AAT_LocalSpaceBase) decode
as per-bone deltas; bake_additive composes them onto the bind pose (the engine blends them
over a runtime base sequence, which offline only the bind pose stands in for). Global-space
additive clips are reported as unsupported.

Native AnimSequence data after the properties (cooked):
    FGuid, FStripDataFlags, bool32 bCooked, i32 CompressedRawDataSize,
    TArray<i32> track -> skeleton bone, i32 bone curve count + 8 byte entries,
    i32 buffer size, bool32 bulk flag, [padding], buffer,
    FString bone codec, FString curve codec, i32 curve bytes + data,
    i32 NumberOfKeys, u8 key format, u8 translation / rotation / scale format,
    i32 byte stream size, i32 track offset count, i32 scale offset count, i32 scale strip size.
The buffer holds the track offsets (per track: translation offset, keys, rotation offset, keys),
the scale offsets (per track: offset, keys) and the key byte stream.

For ACL clips the buffer slot holds a verbatim "compressed_tracks" stream: 8 byte buffer header
(size, hash), 24 byte tracks header (version 10, sample rate, format flags), a 13 x u32 transform
header (region offsets relative to it; segment headers, sub track type bitsets with 2 bits per
track MSB-first, constant track data, clip range data), then per segment: 4 byte padded rotation
bit-rate blocks + 1 byte per vector track, 8 bit segment ranges (multi-segment only) and the
animated bit stream (one frame at a time, groups of 4 animated sub tracks, LSB-first bits,
range-reduced against the segment/clip ranges). Stripped keyframes are interpolated linearly
onto the uniform timeline; default sub tracks (bind pose) are omitted from the tracks.
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
    additive: bool = False                       # tracks hold deltas, not absolute poses

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
    add_type = str(props.get("AdditiveAnimType", ""))
    additive = add_type.endswith(":1")           # AAT_LocalSpaceBase: per-bone local deltas
    if add_type.endswith(("2", "3")):
        raise ValueError("global additive animation (not supported)")
    q = game.read_native_tail(raw, q, a + e.size, "AnimSequence", props, bool(e.object_flags & 0x10))
    d = raw[q:a + e.size]
    p = 16 + 2 + 4                              # FGuid, strip flags, bCooked
    _raw_size, ntr = struct.unpack_from("<ii", d, p)
    p += 8
    track_bones = struct.unpack_from(f"<{ntr}i", d, p)
    p += 4 * ntr
    n_curve, = struct.unpack_from("<i", d, p)
    if n_curve < 0 or n_curve > 10000:
        raise ValueError("bad bone curve count")
    p += 4 + 8 * n_curve                        # cooked FSmartName entries
    size, = struct.unpack_from("<i", d, p)      # NumBytes of the joined buffer
    p += 4
    if size <= 0 or p + size > len(d):          # raw uncompressed or unusual layout
        raise ValueError("uncompressed animation (not supported)")
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
    if codec.startswith("AnimBoneCompressionCodec_ACL"):
        num_keys, acl_tracks = _acl_decode(d[buf:buf + size], track_bones)
        clip = Clip(gpath.rsplit("/", 1)[-1], float(props.get("SequenceLength") or 0.0), num_keys,
                    additive=additive)
        for t, bone in enumerate(track_bones):
            if 0 <= bone < len(bone_names) and t in acl_tracks:
                clip.tracks.setdefault(bone_names[bone], [None, None, None])
                cur = clip.tracks[bone_names[bone]]
                got = acl_tracks[t]
                for i in range(3):
                    if got[i] is not None:
                        cur[i] = got[i]
        return clip
    if codec.startswith("AnimCompress_PerTrackCompression"):
        num_keys, = struct.unpack_from("<i", d, c)
        stream_size, n_off, n_scale, _strip = struct.unpack_from("<iiii", d, c + 8)
        if 4 * n_off + 4 * n_scale + stream_size != size:
            raise ValueError("buffer layout mismatch")
        offsets = struct.unpack_from(f"<{n_off}i", d, buf)
        scale_offsets = struct.unpack_from(f"<{n_scale}i", d, buf + 4 * n_off)
        bs = buf + 4 * n_off + 4 * n_scale
        pt = _pertrack_decode(d, bs, stream_size, offsets, scale_offsets, ntr, num_keys)
        clip = Clip(gpath.rsplit("/", 1)[-1], float(props.get("SequenceLength") or 0.0), num_keys,
                    additive=additive)
        for t, bone in enumerate(track_bones):
            if 0 <= bone < len(bone_names) and t in pt:
                clip.tracks[bone_names[bone]] = pt[t]
        return clip
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

    clip = Clip(gpath.rsplit("/", 1)[-1], float(props.get("SequenceLength") or 0.0), num_keys,
                additive=additive)
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


def bake_additive(clip: Clip, pose: dict) -> Clip:
    """Compose a local-space additive clip's per-bone deltas onto the bind pose
    (pose: bone name -> (quat xyzw, translation cm, scale)), the way the engine combines
    FTransforms: rotation qb x qd, translation tb + R(qb) (sb * td), scale sb * sd.
    The game blends these clips over a runtime base sequence; offline the only pose we
    know is the bind pose, so deltas are applied on top of it. -> a regular Clip with
    absolute tracks."""
    import numpy as np
    out = Clip(clip.name, clip.length, clip.num_keys)
    for bone, (trans, rots, scales) in clip.tracks.items():
        base = pose.get(bone)
        if base is None:
            continue
        qb, tb, sb = (np.asarray(base[0], float), np.asarray(base[1], float),
                      np.asarray(base[2], float))
        n = max(len(trans or ()), len(rots or ()), len(scales or ()))
        if n == 0:
            continue
        td = np.asarray(trans, float) if trans else np.zeros((n, 3))
        qd = np.asarray(rots, float) if rots else np.tile((0.0, 0.0, 0.0, 1.0), (n, 1))
        sd = np.asarray(scales, float) if scales else np.ones((n, 3))
        if not np.any(sd):            # additive scale deltas are stored relative to 1 -> 0 = neutral
            sd = None
        Rb = _trs_matrices(np.asarray([qb], float), np.zeros((1, 3)), np.ones((1, 3)))[0, :3, :3]
        x1, y1, z1, w1 = qb
        x2, y2, z2, w2 = qd[:, 0], qd[:, 1], qd[:, 2], qd[:, 3]
        q = np.column_stack((
            w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
            w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
            w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2))
        q /= np.linalg.norm(q, axis=1)[:, None]
        t = (td * sb) @ Rb.T + tb
        s = None if sd is None else (sd * sb).tolist()
        out.tracks[bone] = (t.tolist(), q.tolist(), s)
    return out


# ---------- PerTrack (AnimCompress_PerTrackCompression) ----------
# Per-track regions in the key stream; region header u32: NumKeys (24 bits, stripped keys
# included), FormatFlags (4 bits, bit 3 = key -> frame table), KeyFormat (4 bits, the engine
# ACF enum). Component counts per format and flags mask follow PerTrackNumComponentTable:
# Float96NoW / Fixed48NoW store the flagged components per key (flags 0 = legacy, all three),
# IntervalFixed32NoW stores fixed f32 min/range pairs plus one 11-11-10-bit packed key.
_PT_COMP_COUNT = {0: [4] * 8, 1: [3, 1, 1, 2, 1, 2, 2, 3], 2: [3, 1, 1, 2, 1, 2, 2, 3],
                  3: [6, 2, 2, 4, 2, 4, 4, 6], 4: [1] * 8, 5: [1] * 8, 6: [0] * 8}
_PT_COMP_SIZE = {0: 4, 1: 4, 2: 2, 3: 4, 4: 4, 5: 4, 6: 0}


def _pt_w(x, y, z):
    return math.sqrt(max(0.0, 1.0 - x * x - y * y - z * z))


def _pt_f32unpack(v, mant_bits):
    """Decode a TFloatPacker<3, mant_bits> value (3 exponent bits, bias 3, exp range [-4, 3])."""
    if v == 0:
        return 0.0
    m = v & ((1 << mant_bits) - 1)
    e = ((v >> mant_bits) & 7) - 4 + 127          # our bias -> IEEE bias
    s = v >> (mant_bits + 3)
    return struct.unpack("<f", struct.pack("<I", (s << 31) | (e << 23) | (m << (23 - mant_bits))))[0]


def _pt_keys(d, p, stride, fmt, flags, keys, fixed, kind):
    """Decode `keys` keys at stream position p; `fixed` = min/range block of format 3."""
    out = []
    for k in range(keys):
        q = p + k * stride
        if fmt == 1:
            if kind == "rot":
                x, y, z = struct.unpack_from("<3f", d, q)
                out.append((x, y, z, _pt_w(x, y, z)))
            elif flags & 7:
                v, off = [], 0
                for i in range(3):
                    if flags & (1 << i):
                        v.append(struct.unpack_from("<f", d, q + off)[0]); off += 4
                    else:
                        v.append(0.0)
                out.append(tuple(v))
            else:
                out.append(struct.unpack_from("<3f", d, q))
        elif fmt == 2:
            if kind == "rot":
                v, off = [0.0] * 3, 0
                for i in range(3):
                    if flags & (1 << i):
                        v[i] = (struct.unpack_from("<H", d, q + off)[0] - 32767) / 32767.0
                        off += 2
                out.append((v[0], v[1], v[2], _pt_w(*v)))
            elif flags & 7:
                v, off = [], 0
                for i in range(3):
                    if flags & (1 << i):
                        v.append(struct.unpack_from("<H", d, q + off)[0] - 255.0); off += 2
                    else:
                        v.append(0.0)
                out.append(tuple(v))
            else:
                out.append(tuple(u - 255.0 for u in struct.unpack_from("<3H", d, q)))
        elif fmt == 3:
            mins, rngs = [0.0] * 3, [0.0] * 3
            cnt = 0
            for i in range(3):
                if flags & (1 << i):
                    mins[i], rngs[i] = struct.unpack_from("<2f", d, fixed + 8 * cnt); cnt += 1
            w, = struct.unpack_from("<I", d, q)
            if kind == "rot":            # FQuatIntervalFixed32NoW: 21..31 X, 10..20 Y, 0..9 Z
                x = (((w >> 21) - 1023) / 1023.0) * rngs[0] + mins[0]
                y = (((w >> 10) & 0x7FF) - 1023) / 1023.0 * rngs[1] + mins[1]
                z = ((w & 0x3FF) - 511) / 511.0 * rngs[2] + mins[2]
                out.append((x, y, z, _pt_w(x, y, z)))
            else:                        # FVectorIntervalFixed32NoW: 0..9 X, 10..20 Y, 21..31 Z
                x = ((w & 0x3FF) - 511) / 511.0 * rngs[0] + mins[0]
                y = (((w >> 10) & 0x7FF) - 1023) / 1023.0 * rngs[1] + mins[1]
                z = (((w >> 21) - 1023) / 1023.0) * rngs[2] + mins[2]
                out.append((x, y, z))
        elif fmt == 4:                   # FQuatFixed32NoW: 11-11-10 bits, W from unit length
            if kind != "rot":
                raise ValueError("fixed-point format not supported for translation/scale")
            wv, = struct.unpack_from("<I", d, q)
            x = ((wv >> 21) - 1023) / 1023.0
            y = (((wv >> 10) & 0x7FF) - 1023) / 1023.0
            z = ((wv & 0x3FF) - 511) / 511.0
            out.append((x, y, z, _pt_w(x, y, z)))
        elif fmt == 5:                   # FQuatFloat32NoW: 11-bit X/Y (7 mantissa), 10-bit Z (6)
            if kind != "rot":
                raise ValueError("float32 format not supported for translation/scale")
            wv, = struct.unpack_from("<I", d, q)
            x = _pt_f32unpack(wv >> 21, 7)
            y = _pt_f32unpack((wv >> 10) & 0x7FF, 7)
            z = _pt_f32unpack(wv & 0x3FF, 6)
            out.append((x, y, z, _pt_w(x, y, z)))
        elif fmt == 0:                   # raw key components (only sensible as four-float quat)
            if kind != "rot":
                raise ValueError("uncompressed key format not supported")
            out.append(struct.unpack_from("<4f", d, q))
        else:                            # ACF_Identity: constant neutral value, no bytes
            out.append((0.0, 0.0, 0.0, 1.0) if kind == "rot"
                       else (1.0, 1.0, 1.0) if kind == "scale" else (0.0, 0.0, 0.0))
    return out


def _pt_sample(vals, n, table):
    """num_keys uniform samples from decoded keys (stripped keys interpolated)."""
    k = len(vals)
    if n <= 1:
        return [vals[0]]
    if k == 1:
        return [vals[0]] * n
    rot = len(vals[0]) == 4
    fr = [float(t) for t in table] if table is not None else [i * (n - 1) / (k - 1) for i in range(k)]
    out = []
    for f in range(n):
        if f <= fr[0]:
            out.append(vals[0])
            continue
        if f >= fr[-1]:
            out.append(vals[-1])
            continue
        i = max(j for j in range(k) if fr[j] <= f)
        a = (f - fr[i]) / max(fr[i + 1] - fr[i], 1.0)
        if rot:
            out.append(_nlerp(vals[i], vals[i + 1], a))
        else:
            out.append(tuple(vals[i][c] + (vals[i + 1][c] - vals[i][c]) * a for c in range(3)))
    return out


def _pt_region(d, bs, stream, off, kind, num_keys):
    hdr, = struct.unpack_from("<i", d, bs + off)
    keys, flags, fmt = hdr & 0xFFFFFF, (hdr >> 24) & 0xF, (hdr >> 28) & 0xF
    if fmt not in _PT_COMP_COUNT:
        raise ValueError(f"key format {fmt} not supported")
    if fmt == 1 and kind == "rot" and (flags & 7) not in (0, 7):
        raise ValueError("per-component float rotation format not supported")
    if fmt == 0 and kind != "rot":
        raise ValueError("uncompressed key format not supported")
    lookup = _PT_COMP_COUNT[fmt][flags & 7]
    n_fixed = lookup if fmt == 3 else 0
    n_comp, comp_sz = (1, 4) if fmt == 3 else (lookup, _PT_COMP_SIZE[fmt])
    if keys == 0:
        return None
    fixed = bs + off + 4
    body = fixed + 4 * n_fixed
    vals = _pt_keys(d, body, n_comp * comp_sz, fmt, flags, keys, fixed, kind)
    end = off + 4 + 4 * n_fixed + keys * n_comp * comp_sz
    table = None
    if flags & 8:                        # frame table, aligned 4 relative to the stream start
        end += (4 - (end & 3)) & 3
        if num_keys > 255:
            table = list(struct.unpack_from(f"<{keys}H", d, bs + end)); end += 2 * keys
        else:
            table = list(struct.unpack_from(f"<{keys}B", d, bs + end)); end += keys
    if end > stream:
        raise ValueError("per-track region out of range")
    return _pt_sample(vals, num_keys, table)


def _pertrack_decode(d, bs, stream, offsets, scale_offsets, ntr, num_keys):
    out = {}
    stride = 2 if len(scale_offsets) >= 2 * ntr else 1
    for t in range(ntr):
        trans = rots = scales = None
        if 2 * t + 1 < len(offsets):
            if offsets[2 * t] >= 0:
                trans = _pt_region(d, bs, stream, offsets[2 * t], "trans", num_keys)
            if offsets[2 * t + 1] >= 0:
                rots = _pt_region(d, bs, stream, offsets[2 * t + 1], "rot", num_keys)
        if t * stride < len(scale_offsets) and scale_offsets[t * stride] >= 0:
            scales = _pt_region(d, bs, stream, scale_offsets[t * stride], "scale", num_keys)
        out[t] = (trans, rots, scales)
    return out


# ---------- ACL (compressed_tracks v02_01_00) ----------
def _read_bits(buf, pos, n):
    v = 0
    for i in range(n):
        v |= ((buf[(pos + i) >> 3] >> ((pos + i) & 7)) & 1) << i
    return v


def _u32f(buf, pos):
    return struct.unpack("<f", struct.pack("<I", _read_bits(buf, pos, 32)))[0]


def _nlerp(a, b, t):
    import numpy as np
    a, b = np.asarray(a, float), np.asarray(b, float)
    if np.dot(a, b) < 0:
        b = -b
    v = a * (1 - t) + b * t
    return tuple(v / np.linalg.norm(v))


def _resample(stored, n, is_rot):
    """Uniform key list from {frame: value} (stripped frames linearly interpolated)."""
    frames = sorted(stored)
    out = []
    for f in range(n):
        v = stored.get(f)
        if v is None:
            lo = max((x for x in frames if x <= f), default=frames[0])
            hi = min((x for x in frames if x >= f), default=frames[-1])
            a = 0.0 if hi == lo else (f - lo) / (hi - lo)
            if is_rot:
                v = _nlerp(stored[lo], stored[hi], a)
            else:
                v = tuple(stored[lo][i] * (1 - a) + stored[hi][i] * a for i in range(3))
        out.append(v)
    return out


def _acl_decode(stream, track_bones):
    """Decode an ACL "compressed_tracks" buffer; {track index: (trans, rots, scales)} lists.

    Supports version 10 (v02_01_00), uniform sampling, qvvf tracks without a database,
    quatf_drop_w_variable rotations and vector3f_variable translations/scales. Values are in
    UE space (cm, LH); default sub tracks are omitted, constant tracks yield one key.
    """
    tag, version, algo, track_type = struct.unpack_from("<IHBB", stream, 8)
    if tag != 0xAC11AC11 or version != 10 or algo != 0 or track_type != 12:
        raise ValueError(f"ACL compressed_tracks tag {tag:#x} v{version} algo {algo} type {track_type}")
    ntr, num_samples = struct.unpack_from("<II", stream, 16)
    if num_samples <= 0:
        raise ValueError("ACL clip has no samples")
    misc = struct.unpack_from("<I", stream, 28)[0]
    has_scale = misc & 1
    scale_fmt, trans_fmt, rot_fmt = (misc >> 2) & 1, (misc >> 3) & 1, (misc >> 4) & 15
    has_db, stripped = (misc >> 8) & 1, (misc >> 10) & 1
    if has_db or (misc >> 30) & 3 or (misc >> 31) & 1:
        raise ValueError("ACL database/optimized streams not supported")
    if rot_fmt != 3 or trans_fmt != 1 or (has_scale and scale_fmt != 1):
        raise ValueError(f"ACL formats rot {rot_fmt}/trans {trans_fmt}/scale {scale_fmt} not supported")
    th = 32
    (nseg, _var, _ra, _ta, _sa, cr_, ct_, cs_,
     _db_off, seg_off, stype_off, cdata_off, range_off) = struct.unpack_from("<13I", stream, th)
    nent = (ntr + 15) // 16

    def bitset(off):
        t = []
        for j in range(nent):
            v = struct.unpack_from("<I", stream, th + off + 4 * j)[0]
            t += [(v >> (30 - 2 * i)) & 3 for i in range(16)]
        return t[:ntr]

    rot_t = bitset(stype_off)
    trans_t = bitset(stype_off + 4 * nent)
    scale_t = bitset(stype_off + 8 * nent) if has_scale else [0] * ntr
    starts = list(struct.unpack_from(f"<{nseg}I", stream, th + 52)) if nseg > 1 else [0]
    hs = 20 if stripped else 16
    segs = []
    for i in range(nseg):
        hb = th + seg_off + i * hs
        pose_bits, _rot_bits, _trans_bits, sd = struct.unpack_from("<4I", stream, hb)
        start = starts[i]
        cnt = (starts[i + 1] - start) if i + 1 < nseg else num_samples - start
        if stripped:
            si = struct.unpack_from("<I", stream, hb + 16)[0]
            stored = [j for j in range(cnt) if (si >> (31 - j)) & 1]     # bitsets are MSB-first
        else:
            stored = list(range(cnt))
        segs.append((start, stored, th + sd, pose_bits))

    o = th + cdata_off
    const_rot, const_trans, const_scale = {}, {}, {}
    for lane in [i for i, t in enumerate(rot_t) if t == 1]:
        x, y, z = struct.unpack_from("<3f", stream, o); o += 12
        const_rot[lane] = (x, y, z, math.sqrt(max(0.0, 1 - x * x - y * y - z * z)))
    for lane in [i for i, t in enumerate(trans_t) if t == 1]:
        const_trans[lane] = struct.unpack_from("<3f", stream, o); o += 12
    for lane in [i for i, t in enumerate(scale_t) if t == 1]:
        const_scale[lane] = struct.unpack_from("<3f", stream, o); o += 12

    # animated rotation sub tracks form groups of 4 consecutive animated tracks (padded)
    rot_lanes = [t for t in range(ntr) if rot_t[t] == 2]
    rot_groups = [rot_lanes[k:k + 4] for k in range(0, len(rot_lanes), 4)]
    trans_lanes = [t for t in range(ntr) if trans_t[t] == 2]
    scale_lanes = [t for t in range(ntr) if scale_t[t] == 2]
    if 4 * len(rot_groups) + len(trans_lanes) + len(scale_lanes) != _var:
        raise ValueError("ACL sub track group layout mismatch")

    clip_rot, clip_vec = {}, {}        # lane -> (min3, extent3)
    if range_off != 0xFFFFFFFF:
        o = th + range_off
        for g in rot_groups:
            gs = len(g)
            c = [struct.unpack_from(f"<{gs}f", stream, o + 4 * gs * k) for k in range(6)]
            o += 24 * gs
            for k, lane in enumerate(g):
                clip_rot[lane] = ((c[0][k], c[1][k], c[2][k]), (c[3][k], c[4][k], c[5][k]))
        for lane in trans_lanes + scale_lanes:
            clip_vec[lane] = (struct.unpack_from("<3f", stream, o),
                              struct.unpack_from("<3f", stream, o + 12))
            o += 24

    samples_rot, samples_trans, samples_scale = {}, {}, {}   # lane -> {global frame: tuple}
    for start, stored, sd, pose_bits in segs:
        o = sd
        rates_rot = {}
        for g in rot_groups:
            blk = struct.unpack_from("<4B", stream, o); o += 4
            for k, lane in enumerate(g):
                rates_rot[lane] = blk[k]
        rates_vec = {}
        for lane in trans_lanes + scale_lanes:
            rates_vec[lane] = stream[o]; o += 1
        seg_rot, seg_vec = {}, {}      # 8 bit segment ranges, normalized against the clip range
        if nseg > 1:
            o = (o + 1) & ~1
            for g in rot_groups:
                blk = stream[o:o + 24]; o += 24
                for k, lane in enumerate(g):
                    seg_rot[lane] = (blk[k], blk[4 + k], blk[8 + k],
                                     blk[12 + k], blk[16 + k], blk[20 + k])
            for lane in trans_lanes + scale_lanes:
                seg_vec[lane] = stream[o:o + 6]; o += 6
        base = ((o + 3) & ~3) * 8
        bits = sum(0 if r == 0 else 3 * r for r in rates_rot.values()) \
            + sum(0 if r == 0 else 3 * r for r in rates_vec.values())
        if bits != pose_bits:
            raise ValueError("ACL animated pose bits mismatch")
        bo = 0
        for j in stored:               # the bit stream is frame-major, groups of 4 per type
            gf = start + j
            for g in rot_groups:
                for lane in g:
                    nb = rates_rot[lane]
                    if nb == 0:
                        raise ValueError("ACL constant bit rate sub tracks not supported")
                    if nb == 32:
                        v = [_u32f(stream, base + bo + 32 * i) for i in range(3)]
                        bo += 96
                    else:
                        q = [_read_bits(stream, base + bo + nb * i, nb) / ((1 << nb) - 1)
                             for i in range(3)]
                        bo += 3 * nb
                        if nseg > 1:
                            b = seg_rot[lane]
                            q = [b[i] / 255.0 + (b[3 + i] / 255.0) * q[i] for i in range(3)]
                        mn, ex = clip_rot.get(lane, ((0, 0, 0), (1, 1, 1)))
                        v = [mn[i] + ex[i] * q[i] for i in range(3)]
                    samples_rot.setdefault(lane, {})[gf] = \
                        tuple(v) + (math.sqrt(max(0.0, 1 - v[0] ** 2 - v[1] ** 2 - v[2] ** 2)),)
            for lane in trans_lanes:
                nb = rates_vec[lane]
                if nb == 0:
                    raise ValueError("ACL constant bit rate sub tracks not supported")
                if nb == 32:
                    v = [_u32f(stream, base + bo + 32 * i) for i in range(3)]
                    bo += 96
                else:
                    q = [_read_bits(stream, base + bo + nb * i, nb) / ((1 << nb) - 1)
                         for i in range(3)]
                    bo += 3 * nb
                    if nseg > 1:
                        b = seg_vec[lane]
                        q = [b[i] / 255.0 + (b[3 + i] / 255.0) * q[i] for i in range(3)]
                    mn, ex = clip_vec.get(lane, ((0, 0, 0), (1, 1, 1)))
                    v = [mn[i] + ex[i] * q[i] for i in range(3)]
                samples_trans.setdefault(lane, {})[gf] = tuple(v)
            for lane in scale_lanes:
                nb = rates_vec[lane]
                if nb == 0:
                    raise ValueError("ACL constant bit rate sub tracks not supported")
                if nb == 32:
                    v = [_u32f(stream, base + bo + 32 * i) for i in range(3)]
                    bo += 96
                else:
                    q = [_read_bits(stream, base + bo + nb * i, nb) / ((1 << nb) - 1)
                         for i in range(3)]
                    bo += 3 * nb
                    if nseg > 1:
                        b = seg_vec[lane]
                        q = [b[i] / 255.0 + (b[3 + i] / 255.0) * q[i] for i in range(3)]
                    mn, ex = clip_vec.get(lane, ((0, 0, 0), (1, 1, 1)))
                    v = [mn[i] + ex[i] * q[i] for i in range(3)]
                samples_scale.setdefault(lane, {})[gf] = tuple(v)
        if bo != bits * len(stored):
            raise ValueError("ACL animated bit stream size mismatch")

    tracks = {}
    for t in range(ntr):
        rot = trans = scale = None
        if rot_t[t] == 2:
            rot = _resample(samples_rot[t], num_samples, True)
        elif t in const_rot:
            rot = [const_rot[t]]
        if trans_t[t] == 2:
            trans = _resample(samples_trans[t], num_samples, False)
        elif t in const_trans:
            trans = [const_trans[t]]
        if scale_t[t] == 2:
            scale = _resample(samples_scale[t], num_samples, False)
        elif t in const_scale:
            scale = [const_scale[t]]
        if rot is not None or trans is not None or scale is not None:
            tracks[t] = (trans, rot, scale)
    return num_samples, tracks


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
