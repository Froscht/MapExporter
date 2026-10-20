"""Textures: decode cooked Texture2D mips (BCn / uncompressed via Pillow) and find the base-color
texture of a material instance.

Cooked UE5 Texture2D, native part after the properties:
    ..., SizeX, SizeY, PackedData (i32 each), FString pixel format ("PF_DXT5"), i32, i32 mip count,
    per mip: i32 bulk data index (into the package's bulk data map), SizeX, SizeY, SizeZ.
Large mips live in the .ubulk file, small ones inline in the package. Texture LOD bias can drop
the largest mips, so the first listed mip may be smaller than SizeX/SizeY.
"""
from __future__ import annotations

import io
import struct

from . import game

# pixel format -> (Pillow decoder, decoder argument, mode)
FORMATS = {
    "PF_DXT1": ("bcn", 1, "RGBA"), "PF_DXT3": ("bcn", 2, "RGBA"), "PF_DXT5": ("bcn", 3, "RGBA"),
    "PF_BC4": ("bcn", 4, "L"), "PF_BC5": ("bcn", 5, "RGB"), "PF_BC7": ("bcn", 7, "RGBA"),
    "PF_B8G8R8A8": ("raw", "BGRA", "RGBA"), "PF_R8G8B8A8": ("raw", "RGBA", "RGBA"), "PF_G8": ("raw", "L", "L"),
}
# texture parameter names that hold the base color, best first
BASE_COLOR_PARAMS = ("BaseColor", "Base Color", "BaseColorTexture", "Albedo", "Diffuse", "CR", "Color", "Colour")


def _texture_header(raw, pkg, e):
    """(pixel format, [(bulk index, w, h)]) of a Texture2D export."""
    a = pkg.header_size + e.offset
    d = raw[a:a + e.size]
    i = d.find(b"PF_")
    if i < 16:
        raise ValueError("no pixel format")
    n, = struct.unpack_from("<i", d, i - 4)
    fmt = d[i:i + n - 1].decode("ascii", "replace")
    p = i + n + 4
    nmips, = struct.unpack_from("<i", d, p)
    p += 4
    if not 0 < nmips <= 16:
        raise ValueError(f"mip count {nmips}")
    mips = [struct.unpack_from("<iii", d, p + 16 * k) for k in range(nmips)]
    return fmt, mips


def load_texture(gpath: str, max_size: int = 2048):
    """Largest mip <= max_size of a Texture2D as a Pillow image (RGBA or L), or None."""
    from PIL import Image
    from . import models
    raw, pkg = models.package(gpath)
    e = next((e for e in pkg.exports if pkg.export_class(e, script_names=game.smap) == "Texture2D"), None)
    if e is None:
        return None
    fmt, mips = _texture_header(raw, pkg, e)
    if fmt not in FORMATS:
        return None
    bm = pkg.bulk_data_map() or []
    for bulk_index, w, h in mips:
        if max(w, h) > max_size or not 0 <= bulk_index < len(bm):
            continue
        off, _dup, size, flags, _ci = bm[bulk_index]
        if flags & 0x100:                       # stored in the .ubulk file
            hits = models.lookup(models.to_key(gpath, ".ubulk"))
            if not hits:
                continue
            c, ci = hits[0]
            data = c.get_chunk(ci)[off:off + size]
        else:                                   # inline: after the export data of the package
            data = raw[pkg.header_size + _exports_end(pkg) + off:][:size]
        dec, arg, mode = FORMATS[fmt]
        try:
            return Image.frombytes(mode, (w, h), bytes(data), dec, arg)
        except Exception:
            return None
    return None


def _exports_end(pkg):
    return max(e.offset + e.size for e in pkg.exports)


def material_params(mat_gpath: str, depth: int = 0):
    """(textures {name: /Game path}, vectors {name: (r, g, b, a)}) of a material instance,
    merged along the parent chain (the child overrides its parents)."""
    from . import models
    if depth > 8 or not mat_gpath or not models.exists(mat_gpath):
        return {}, {}
    raw, pkg = models.package(mat_gpath)
    tex, vec, parent = {}, {}, None
    for e, cls, props in models.exports(mat_gpath):
        if not props or not cls or "Material" not in cls:
            continue
        for t in props.get("TextureParameterValues") or []:
            d = dict(t)
            name = dict(d.get("ParameterInfo") or ()).get("Name")
            ref = game.ref(pkg, d.get("ParameterValue")) if isinstance(d.get("ParameterValue"), int) else None
            if isinstance(name, int) and name < len(pkg.names) and ref and ref[0] == "import":
                tex[pkg.names[name]] = ref[1]
        for t in props.get("VectorParameterValues") or []:
            d = dict(t)
            name = dict(d.get("ParameterInfo") or ()).get("Name")
            if isinstance(name, int) and name < len(pkg.names) and isinstance(d.get("ParameterValue"), tuple):
                vec[pkg.names[name]] = d["ParameterValue"]
        r = game.ref(pkg, props.get("Parent")) if isinstance(props.get("Parent"), int) else None
        if r and r[0] == "import":
            parent = r[1]
    ptex, pvec = material_params(parent, depth + 1) if parent else ({}, {})
    ptex.update(tex)
    pvec.update(vec)
    return ptex, pvec


def material_texture(mat_gpath: str):
    """Base-color texture (/Game/... path) of a material instance, or None."""
    tex, _vec = material_params(mat_gpath)
    for want in BASE_COLOR_PARAMS:
        for name, path in tex.items():
            if name.lower() == want.lower():
                return path
    return None


def _linear_to_srgb(c):
    import numpy as np
    c = np.clip(c, 0.0, 1.0)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)


def material_albedo(mat_gpath: str, max_size: int = 2048):
    """Color image (RGB) for a material instance, or None.

    Character outfit materials are layered: their "BaseColor" texture is a non-sRGB detail map
    (R: shading, G: wear) and the colors come from the color-scheme vectors ColorA/B/C, placed by
    the ColorMask texture (G -> ColorB, B -> ColorC over ColorA). That is rebuilt here; other
    materials use their base-color texture directly."""
    from PIL import Image
    tex, vec = material_params(mat_gpath)
    if "ColorMask" in tex and "BaseColor" in tex and all(k in vec for k in ("ColorA", "ColorB", "ColorC")):
        import numpy as np
        detail = load_texture(tex["BaseColor"], max_size)
        mask = load_texture(tex["ColorMask"], max_size)
        if detail is not None and mask is not None:
            if mask.size != detail.size:
                mask = mask.resize(detail.size)
            bc = np.asarray(detail.convert("RGB"), np.float32) / 255.0
            cm = np.asarray(mask.convert("RGBA"), np.float32) / 255.0
            a, b, c = (np.asarray(vec[k][:3], np.float32) for k in ("ColorA", "ColorB", "ColorC"))
            col = np.broadcast_to(a, bc.shape).copy()
            col = col * (1 - cm[..., 1:2]) + b * cm[..., 1:2]
            col = col * (1 - cm[..., 2:3]) + c * cm[..., 2:3]
            shade = bc[..., 0:1] * bc[..., 1:2]
            out = _linear_to_srgb(col * (0.25 + 0.75 * shade))
            return Image.fromarray((out * 255 + 0.5).astype(np.uint8), "RGB")
    path = material_texture(mat_gpath)
    img = load_texture(path, max_size) if path else None
    return img.convert("RGB") if img is not None else None


def to_png(img, opaque=True) -> bytes:
    """PNG bytes; opaque drops alpha (base-color textures often keep roughness/masks in alpha)."""
    if opaque or img.mode != "RGBA":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=False, compress_level=6)
    return buf.getvalue()
