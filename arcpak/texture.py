"""Textures: decode cooked Texture2D mips (BCn / uncompressed via Pillow) and find the base-color
texture of a material instance.

Cooked UE5 Texture2D, native part after the properties (tightly packed, so at any byte alignment):
    ..., SizeX, SizeY, PackedData (i32 each), FString pixel format ("PF_DXT5"), u32 0, u32 mip count,
    then 16-byte mip records ONLY for the mips stored in .ubulk — a fully inline texture has no
records at all. The reliable per-mip data is the package's bulk data map (one entry per mip, in mip
order); sizes are validated against SizeX/SizeY halved per level. Inline SerialOffset counts from
the export data, .ubulk SerialOffset (flag 0x100) from the .ubulk file. LOD bias can drop the
largest mips, so the first bulk entry may match a deeper mip level than 0.
"""
from __future__ import annotations

import io
import json
import os
import re
import struct
import sys
from dataclasses import dataclass

from . import game
from .config import report_progress

# pixel format -> (Pillow decoder, decoder argument, mode)
FORMATS = {
    "PF_DXT1": ("bcn", 1, "RGBA"), "PF_DXT3": ("bcn", 2, "RGBA"), "PF_DXT5": ("bcn", 3, "RGBA"),
    "PF_BC4": ("bcn", 4, "L"), "PF_BC5": ("bcn", 5, "RGB"), "PF_BC7": ("bcn", 7, "RGBA"),
    "PF_B8G8R8A8": ("raw", "BGRA", "RGBA"), "PF_R8G8B8A8": ("raw", "RGBA", "RGBA"), "PF_G8": ("raw", "L", "L"),
}
# texture parameter names that hold the base color, best first
BASE_COLOR_PARAMS = ("BaseColor", "Base Color", "BaseColorTexture", "Albedo", "Diffuse", "CR", "Color", "Colour")


# bytes per 4x4 block (BC formats) or per pixel (uncompressed), for bulk-size validation
_BLOCK = {"PF_DXT1": 8, "PF_DXT3": 16, "PF_DXT5": 16, "PF_BC4": 8, "PF_BC5": 16, "PF_BC7": 16,
          "PF_B8G8R8A8": 4, "PF_R8G8B8A8": 4, "PF_G8": 1}


def _mip_size(fmt, w, h):
    """Bytes of a w x h mip; None for pixel formats whose layout is unknown."""
    blk = _BLOCK.get(fmt)
    if blk is None:
        return None
    if fmt in ("PF_B8G8R8A8", "PF_R8G8B8A8", "PF_G8"):
        return w * h * blk
    return -(-w // 4) * -(-h // 4) * blk


def _pixel_format(d):
    """Pixel format string from a cooked Texture2D export region, without validating anything else."""
    i = d.find(b"PF_")
    if i < 4:
        return None
    n, = struct.unpack_from("<i", d, i - 4)
    if not 4 <= n <= 16:
        return None
    return d[i:i + n - 1].decode("ascii", "replace")


def _texture_header(raw, pkg, e):
    """(pixel format, [(bulk index, offset, size, flags, w, h)]) of a Texture2D export.

    Reads SizeX/SizeY and the pixel format string from the tail (they sit right before the
    format string at fixed distances), then derives the mips from the bulk data map: each entry
    must equal one mip of the halving size chain, strictly increasing in level (that resolves the
    tail mips where 4x4, 2x2 and 1x1 have the same block count). Raises for formats outside
    FORMATS (data textures like PF_FloatRGBA VATs) and for headers that don't validate."""
    a = pkg.header_size + e.offset
    d = raw[a:a + e.size]
    i = d.find(b"PF_")
    if i < 20:
        raise ValueError("no pixel format")
    n, = struct.unpack_from("<i", d, i - 4)
    if not 4 <= n <= 16:
        raise ValueError("bad pixel format string")
    fmt = d[i:i + n - 1].decode("ascii", "replace")
    if fmt not in _BLOCK:
        raise ValueError(f"format {fmt}")
    sx, sy = struct.unpack_from("<ii", d, i - 16)
    if not (0 < sx <= 16384 and 0 < sy <= 16384):
        raise ValueError(f"size {sx}x{sy}")
    bm = pkg.bulk_data_map() or []
    if not bm:
        raise ValueError("no bulk data map")
    mips, lvl = [], 0
    for k, (off, _dup, size, flags, _ci) in enumerate(bm):
        for level in range(lvl, 17):
            w, h = max(1, sx >> level), max(1, sy >> level)
            if _mip_size(fmt, w, h) == size:
                mips.append((k, off, size, flags, w, h))
                lvl = level + 1
                break
        else:
            raise ValueError(f"bulk entry {k}: {size} bytes fit no mip of {sx}x{sy} {fmt}")
    return fmt, mips


def load_texture(gpath: str, max_size: int = 2048, display: bool = False):
    """Largest mip <= max_size of a Texture2D as a Pillow image (RGBA or L), or None.

    display=True: prepare for display — linear-stored color textures get gamma-encoded
    (colorspace() + for_display()). The raw call stays raw: material_albedo needs linear values."""
    from PIL import Image
    from . import models
    raw, pkg = models.package(gpath)
    e = next((e for e in pkg.exports if pkg.export_class(e, script_names=game.smap) == "Texture2D"), None)
    if e is None:
        return None
    try:
        fmt, mips = _texture_header(raw, pkg, e)
    except ValueError:                          # unsupported data formats, unreadable tail => no image
        return None
    if fmt not in FORMATS:
        return None
    def grab(k, off, size, flags, w, h):
        if flags & 0x100:                       # stored in the .ubulk file
            hits = models.lookup(models.to_key(gpath, ".ubulk"))
            if not hits:
                return None
            c, ci = hits[0]
            data = c.get_chunk(ci)[off:off + size]
        else:                                   # inline: SerialOffset counts from the export data
            data = raw[pkg.header_size + e.offset + off:][:size]
        dec, arg, mode = FORMATS[fmt]
        try:
            return Image.frombytes(mode, (w, h), bytes(data), dec, arg)
        except Exception:                       # a broken chunk must not kill the remaining mips
            return None

    for k, off, size, flags, w, h in mips:      # mip order: first entry that fits max_size wins
        if max(w, h) > max_size:
            continue
        img = grab(k, off, size, flags, w, h)
        if img is not None:
            return for_display(gpath, img) if display else img
    if mips:                                    # cooked without mips (many UI textures): their only
        k, off, size, flags, w, h = mips[-1]    # mip is bigger than max_size — use it, don't show nothing
        img = grab(k, off, size, flags, w, h)
        if img is not None:
            return for_display(gpath, img) if display else img
    return None


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


# Color space of cooked textures: the SRGB property is only stored when it deviates from the
# archetype (absent => sRGB, raw bytes are displayable as-is; SRGB=0 => linear, colors look dark
# until gamma-encoded). Normal maps and masks are data and never get gamma. Names decide, because
# a linear-flagged texture can still hold data (e.g. masks stored in a "DefaultGroup" texture).
_DATA_NAME_RE = re.compile(r"MASK|NORM|NOX|_NX|NOM_|ORM|CURV|SPLAT|PACK|AO|ROUGH|OPA|THICK|WEAR|"
                           r"FLOW|HYDR|_ID|_ID_|DETAIL", re.I)
_COLOR_NAME_RE = re.compile(r"BASECOL|BASE_COLOR|ALBEDO|DIFFUSE|COLOU?R", re.I)   # COLOR and COLOUR
# An unambiguous albedo word beats a data word: clothing items called "Facemask" carry real
# albedos (T_Facemask_BaseColor), while genuine masks ("…ColorMask", packed "…_M") match no
# albedo word and stay raw.
_STRONG_COLOR_RE = re.compile(r"BASECOL|BASE_COLOR|ALBEDO|DIFFUSE|DECALCOL", re.I)
_cs_cache: dict[str, tuple[bool, bool]] = {}


def _looks_like_color(stem: str) -> bool:
    """Name gate for linear-stored textures: gamma-lift only textures that hold color."""
    if _STRONG_COLOR_RE.search(stem):
        return True
    return bool(_COLOR_NAME_RE.search(stem)) and not _DATA_NAME_RE.search(stem)


def colorspace(gpath: str) -> tuple[bool, bool]:
    """(stored as sRGB?, normal map?) from the Texture2D properties (cached).

    Unknown schema or unreadable package => assume sRGB so bytes stay untouched."""
    hit = _cs_cache.get(gpath)
    if hit is not None:
        return hit
    srgb, normal = True, False
    try:
        from . import models
        raw, pkg = models.package(gpath)
        e = next((e for e in pkg.exports if pkg.export_class(e, script_names=game.smap) == "Texture2D"), None)
        if e is not None:
            a = pkg.header_size + e.offset
            _q, props = game.read_unversioned(raw, a, a + e.size, "Texture2D")
            d = dict(props)
            srgb = d.get("SRGB") != 0
            comp = d.get("CompressionSettings")
            normal = isinstance(comp, str) and comp.rsplit(":", 1)[-1] == "1"   # TC_Normalmap
    except Exception:
        pass
    _cs_cache[gpath] = (srgb, normal)
    return _cs_cache[gpath]


def _bc5_display(img):
    """PF_BC5 carries two signed channels (normal maps): Pillow puts the two BC4 planes into R and G
    and leaves B empty, which looks olive. For display only, reconstruct Z = sqrt(1-x^2-y^2) into B."""
    import numpy as np
    from PIL import Image
    c = np.asarray(img, np.float32) / 255.0
    x, y = c[..., 0] * 2 - 1, c[..., 1] * 2 - 1
    z = np.sqrt(np.clip(1.0 - x * x - y * y, 0.0, 1.0))
    c[..., 2] = (z + 1.0) * 0.5
    return Image.fromarray((c * 255 + 0.5).astype(np.uint8), "RGB")


def for_display(gpath: str, img):
    """Prepare a decoded texture for PNG/display: linear-stored color textures (SRGB=0) are
    gamma-encoded; sRGB textures, normal maps and data textures (masks …) pass through unchanged.

    load_texture deliberately stays raw — material_albedo needs the linear values."""
    import numpy as np
    from PIL import Image
    if img is None:
        return img
    if img.mode == "RGB":
        return _bc5_display(img)                # PF_BC5 is the only format decoding to plain RGB
    if img.mode != "RGBA":
        return img
    srgb, normal = colorspace(gpath)
    if srgb or normal:
        return img
    stem = gpath.rsplit("/", 1)[-1]
    if not _looks_like_color(stem):
        return img
    a = np.asarray(img, np.float32) / 255.0
    a[..., :3] = _linear_to_srgb(a[..., :3])
    return Image.fromarray((np.clip(a, 0, 1) * 255 + 0.5).astype(np.uint8), img.mode)


def colorspace_label(gpath: str) -> str:
    """German display text of how the texture is stored (for info dialogs)."""
    srgb, normal = colorspace(gpath)
    if normal:
        return "Normaldaten"
    if srgb:
        return "sRGB"
    stem = gpath.rsplit("/", 1)[-1]
    if _looks_like_color(stem):
        return "linear (Farbe → entzerrt)"
    return "linear (Daten)"


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
    return for_display(path, img).convert("RGB") if img is not None else None


def to_png(img, opaque=True) -> bytes:
    """PNG bytes; opaque drops alpha (base-color textures often keep roughness/masks in alpha)."""
    if opaque or img.mode != "RGBA":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=False, compress_level=6)
    return buf.getvalue()


# ---------- catalogue --------------------------------------------------------------------------
@dataclass
class Item:
    name: str
    gpath: str                                  # /Game/... of the texture package
    folder: str = ""                            # grouping label below Content
    size: int = 0                               # .uasset bytes
    bulk: int = 0                               # .ubulk bytes (0 for small inline textures)


def _folder(key: str) -> str:
    from . import models
    tail = key[len(models.CONTENT):] if key.startswith(models.CONTENT) else key
    if "/Content/" in tail:                     # engine mounts (../../../Engine/Content/…)
        tail = tail.split("/Content/", 1)[1]
    head, _, _ = tail.rpartition("/")
    return head


def _size(key: str) -> int:
    """Chunk length of a container entry, without decompressing it."""
    from . import models
    hits = models.lookup(key)
    return 0 if not hits else int(hits[0][0].chunk_offsets[hits[0][1]][1])


def catalogue(limit: int = 0) -> list[Item]:
    """Every cooked texture: name, package path, folder and file sizes (no parsing, ~1 s).

    Filtered by the cooked name convention ('T_…') and by the game content mount (engine textures
    have another mount point and cannot be addressed through a /Game path). Reading the package to
    test for a Texture2D export would cost ~15 s for the whole game, and export reports
    non-textures anyway.
    """
    from . import models
    keys = sorted(k for k in models.index()
                  if k.startswith(models.CONTENT) and k.endswith(".uasset")
                  and k.rsplit("/", 1)[-1].startswith("T_"))
    if limit:
        keys = keys[:limit]
    out: list[Item] = []
    for n, key in enumerate(keys):
        stem = key.rsplit("/", 1)[-1][:-len(".uasset")]
        bkey = models.to_key(models.to_gpath(key), ".ubulk")
        out.append(Item(name=stem, gpath=models.to_gpath(key), folder=_folder(key),
                        size=_size(key), bulk=_size(bkey)))
        if n % 1000 == 999:
            report_progress(n + 1, len(keys))
    report_progress(len(keys), len(keys))
    return out


def find(name: str, cat: list[Item] | None = None) -> list[Item]:
    """Catalogue items matching a name: exact first, then substring, case-insensitive."""
    cat = catalogue() if cat is None else cat
    n = name.lower()
    return [it for it in cat if it.name.lower() == n] or [it for it in cat if n in it.name.lower()]


def texture_info(gpath: str) -> dict:
    """Pixel format and mips (bulk index, size, where the bytes live) of a Texture2D."""
    from . import models
    raw, pkg = models.package(gpath)
    e = next((e for e in pkg.exports if pkg.export_class(e, script_names=game.smap) == "Texture2D"), None)
    if e is None:
        raise ValueError(f"kein Texture2D in {gpath}")
    a = pkg.header_size + e.offset
    try:
        fmt, mips = _texture_header(raw, pkg, e)
    except ValueError:                          # e.g. PF_FloatRGBA data textures: format name only
        fmt = _pixel_format(raw[a:a + e.size]) or "?"
        mips = []
    out = [{"w": w, "h": h, "bytes": size, "ort": "ubulk" if flags & 0x100 else "inline"}
           for _k, _off, size, flags, w, h in mips]
    return {"pfad": gpath, "format": fmt, "dekodierbar": fmt in FORMATS, "farbraum": colorspace_label(gpath),
            "mips": out, "bytes": sum(m["bytes"] for m in out)}


# ---------- export ----------------------------------------------------------------------------
def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text or "").strip("_")


def out_path(item, ext: str = ".png", base: str | None = None) -> str:
    from . import models
    base = base or os.path.join(models.HERE, "out", "textures")
    sub = _safe(getattr(item, "folder", ""))
    name = _safe(getattr(item, "name", "") or str(item).rsplit("/", 1)[-1])
    return os.path.join(base, sub, name + ext) if sub else os.path.join(base, name + ext)


def export_png(item_or_gpath, path: str | None = None, max_size: int = 2048, opaque: bool = True) -> str:
    """Write the largest mip up to max_size of one texture as PNG."""
    gpath = item_or_gpath if isinstance(item_or_gpath, str) else item_or_gpath.gpath
    if path is None:
        path = out_path(item_or_gpath)
    img = for_display(gpath, load_texture(gpath, max_size))
    if img is None:
        raise ValueError("keine Textur dekodierbar (Format unsupported oder kein Texture2D)")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(to_png(img, opaque=opaque))
    return path


# ---------- command line ----------------------------------------------------------------------
HELP = """textures – Texture-Katalog und PNG-Export

    python tools/mapexport.py textures list   [--filter=NAME] [--limit=N] [--json=DATEI]
    python tools/mapexport.py textures info   NAME
    python tools/mapexport.py textures export NAME [NAME …] [--out=ORDNER] [--max=2048] [--alpha]
    python tools/mapexport.py textures export --alle [--filter=NAME] [--limit=N]

list zeigt alle gekochten Texturen (Name 'T_…') mit Dateigrößen, info die MIPS eines Pakets und
das Pixelformat. export schreibt das größte Mip bis --max (Standard 2048) als PNG; Texturen, die
ohne Mip-Kette gekocht sind (viele UI-Bilder), nehmen ihren einzigen Mip auch wenn er größer ist;
--alpha behält
den Alphakanal (Roughness/Masken sitzen oft darin). Dekodiert werden BC1/3/5/BC4/BC5/BC7 und
unkomprimierte Formate — alles, was ARC verwendet.
"""


def main(argv=None):
    import time
    from . import models
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        raise SystemExit(HELP)
    cmd, rest = argv[0], argv[1:]
    flags = [a for a in rest if a.startswith("--")]
    words = [a for a in rest if not a.startswith("--")]

    def opt(name, default=None):
        return next((a[len(name) + 1:] for a in flags if a.startswith(name)), default)

    if cmd not in ("list", "info", "export"):
        raise SystemExit(HELP)

    t0 = time.time()
    flt = (opt("--filter") or "").lower()
    cat = [it for it in catalogue(int(opt("--limit") or 0))
           if not flt or flt in it.name.lower() or flt in it.folder.lower()]

    if cmd == "list":
        print(f"{len(cat)} Texturen ({time.time() - t0:.0f} s), Suche '{flt or '—'}'")
        for it in cat:
            print(f"{max(it.size, it.bulk) / 1e6:7.3f} MB  {it.name}  [{it.folder}]")
        if (jp := opt("--json")):
            os.makedirs(os.path.dirname(jp) or ".", exist_ok=True)
            with open(jp, "w", encoding="utf-8") as f:
                json.dump([{"name": it.name, "pfad": it.gpath, "ordner": it.folder,
                            "bytes": it.size, "ubulk_bytes": it.bulk} for it in cat], f,
                          ensure_ascii=False, indent=1)
            print("Katalog geschrieben:", jp)
        return

    if cmd == "info":
        if not words:
            raise SystemExit("info braucht einen Asset-Namen")
        hits = find(words[0], cat)
        if not hits:
            raise SystemExit(f"keine Textur '{words[0]}'")
        try:
            print(json.dumps(texture_info(hits[0].gpath), ensure_ascii=False, indent=1))
        except Exception as e:
            raise SystemExit(f"{hits[0].name}: {type(e).__name__}: {e}")
        return

    names = [w.lower() for w in words]
    todo = cat if (not names or "alle" in names) else [it for it in cat
                                                       if any(n == it.name.lower() or n in it.name.lower() for n in names)]
    out_dir = opt("--out") or os.path.join(models.HERE, "out", "textures")
    max_size = int(opt("--max") or 2048)
    opaque = "--alpha" not in flags
    done = fails = 0
    total = 0
    for n, it in enumerate(todo):
        try:
            p = export_png(it, out_path(it, base=out_dir), max_size=max_size, opaque=opaque)
            total += os.path.getsize(p)
            done += 1
        except Exception as e:
            fails += 1
            print(f"FEHLER {it.name}: {type(e).__name__}: {e}")
        if n % 200 == 199:
            report_progress(n + 1, len(todo))
    report_progress(len(todo), len(todo))
    print(f"{done} exportiert ({total / 1e6:.1f} MB) nach {out_dir}, {fails} Fehler, {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
