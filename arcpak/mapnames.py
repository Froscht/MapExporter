"""Map display names from the game files.

Source 1 (always available): the string tables in IoStore - English source text, e.g.
  ST_MapSelection: ID_MAPSELECTION_DAM_FULL = "Dam Battlegrounds"
  ST_Location_RivenTides: ID_LOCATION_RIVENTIDES = "Riven Tides"
Source 2 (optional): the translations in Localization/Game/<culture>/Game.locres. These live in
the classic .pak files, whose index is encrypted with the AES key from settings.json.
If the key is wrong (e.g. after a patch), the English names are used silently.
"""
from __future__ import annotations

import re
import struct

# GUI language -> UE culture
CULTURES = {"de": "de", "en": "en", "fr": "fr", "es": "es-ES", "it": "it", "pl": "pl",
            "pt": "pt-BR", "ru": "ru", "zh": "zh-Hans", "ja": "ja"}

_TABLES = ("/ST_MapSelection.uasset", "/ST_Location.uasset", "/ST_Location_")


def _fstr(b, p):
    n = struct.unpack_from("<i", b, p)[0]
    p += 4
    if n < 0:
        return bytes(b[p:p - 2 * n]).decode("utf-16-le", "replace").rstrip("\x00"), p - 2 * n
    return bytes(b[p:p + n]).decode("utf-8", "replace").rstrip("\x00"), p + n


def parse_string_table(data: bytes, table: str) -> dict[str, str]:
    """FStringTable export: FString namespace, i32 count, (FString key, FString text)*."""
    ns = table.encode() + b"\x00"
    pat = re.escape(struct.pack("<i", len(ns)) + ns)
    for m in re.finditer(pat, data):
        try:
            q = m.end()
            n = struct.unpack_from("<i", data, q)[0]
            q += 4
            if not 0 < n < 100000:
                continue
            out = {}
            for _ in range(n):
                k, q = _fstr(data, q)
                v, q = _fstr(data, q)
                out[k] = v
            return out
        except (struct.error, UnicodeDecodeError):
            continue
    return {}


def string_tables(containers) -> dict[str, dict[str, str]]:
    from .iostore import package_entries
    out = {}
    for c, path, ud in package_entries(containers, "/Localization/ST_"):
        if not any(t in path for t in _TABLES):
            continue
        name = path.rsplit("/", 1)[-1][:-len(".uasset")]
        try:
            tbl = parse_string_table(c.get_chunk(ud), name)
        except Exception:
            continue
        if tbl:
            out[name] = tbl
    return out


# ---------- .locres ----------
LOCRES_MAGIC = struct.pack("<4I", 0x7574140E, 0xFC034A67, 0x9D90154A, 0x1B7F37C3)


def parse_locres(b: bytes) -> dict[tuple[str, str], str]:
    """(namespace, key) -> translated text. Versions 0-3."""
    p, ver = 0, 0
    if b[:16] == LOCRES_MAGIC:
        ver = b[16]
        p = 17
    strings = []
    if ver >= 1:
        soff = struct.unpack_from("<q", b, p)[0]
        p += 8
        q = soff
        n = struct.unpack_from("<i", b, q)[0]
        q += 4
        for _ in range(n):
            s, q = _fstr(b, q)
            if ver >= 3:
                q += 4
            strings.append(s)
    if ver >= 2:
        p += 4                                   # total entry count
    out = {}
    nns = struct.unpack_from("<I", b, p)[0]
    p += 4
    for _ in range(nns):
        if ver >= 2:
            p += 4
        ns, p = _fstr(b, p)
        nk = struct.unpack_from("<I", b, p)[0]
        p += 4
        for _ in range(nk):
            if ver >= 2:
                p += 4
            key, p = _fstr(b, p)
            p += 4                               # source string hash
            if ver >= 1:
                idx = struct.unpack_from("<i", b, p)[0]
                p += 4
                txt = strings[idx] if 0 <= idx < len(strings) else ""
            else:
                txt, p = _fstr(b, p)
            out[(ns, key)] = txt
    return out


def load_locres(pak_dir, aes_keys, culture: str) -> dict[tuple[str, str], str]:
    from .pakfile import open_paks
    want = f"/Localization/Game/{culture}/Game.locres"
    for pk in open_paks(pak_dir, aes_keys, quiet=True):
        try:
            for name in pk.find(want):
                return parse_locres(pk.read(name))
        finally:
            pk.close()
    return {}


# ---------- map folder -> name mapping ----------
def _keys_for(folder: str):
    stem = re.sub(r"_\d+$", "", folder)
    base = stem.upper()
    variants = [base]
    if base.startswith("THE"):
        variants.append(base[3:])
    if base.endswith("ARRAY"):
        variants.append(base[:-5])
    for v in variants:
        for suffix in ("FULL", "SHORT", "TITLE"):
            yield "ST_MapSelection", f"ID_MAPSELECTION_{v}_{suffix}"
        yield f"ST_Location_{stem}", f"ID_LOCATION_{v}"
        yield "ST_Location", f"ID_LOCATION_{v}"


def map_names(containers, folders, lang: str = "en", pak_dir=None, aes_keys=()) -> dict[str, str]:
    """{map folder: display name}; only maps the game has a name for."""
    tables = string_tables(containers)
    loc = {}
    culture = CULTURES.get(lang, lang)
    if culture != "en" and pak_dir:
        try:
            loc = load_locres(pak_dir, aes_keys, culture)
        except Exception:
            loc = {}
    out = {}
    for f in folders:
        for table, key in _keys_for(f):
            src = tables.get(table, {}).get(key)
            if src:
                out[f] = loc.get((table, key)) or src
                break
    return out


if __name__ == "__main__":
    import sys
    from .config import Config
    from .iostore import mount_directory
    cfg = Config.load()
    cs = mount_directory(cfg.pak_dir)
    folders = sorted({m.group(1) for c in cs for p, _ in c.entries
                      for m in [re.search(r"Maps/([^/]+)/", p)] if m})
    lang = sys.argv[1] if len(sys.argv) > 1 else "en"
    for k, v in map_names(cs, folders, lang, cfg.pak_dir, cfg.aes_keys).items():
        print(f"{k:24s} {v}")
