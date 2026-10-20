"""arcpak.globaldata — ScriptObjects (script-import) name resolution.

global.utoc v8 layout: 144-byte header, AES-ECB-encrypted body;
  chunk 0 is the ScriptObjects chunk (type 5), its content spans several compression blocks;
  the block table (12 bytes per entry) follows the chunk ids/offsets and the seeds.
global.ucas: Theia-encrypted (global.ucas.meta sidecar, real file size).
ScriptObjects blob layout:
  FName batch:  u32 count, u32 strbytes, u64 hashver, 8*count hashes,
                count x u16 FSerializedNameHeader (big-endian: bit 15 = IsWide, low 15 bits = length),
                then the string bytes
  then i32 scriptObjectCount + count x FScriptObjectEntry, stride 32:
       u32 mappedName, pad4, u64 globalIndex, u64 outerIndex, u64 cdoIndex
Script-import ids in packages equal GlobalIndex as RAW u64 (type bits
included) - never mask either side.
"""
from __future__ import annotations
import os
import struct
from .iostore import HARDCODED_KEY_DEFAULT, ecb_decrypt, align
from .crypto import TheiaDecryptor

from .config import Config

PAKS_DEFAULT = Config.load().pak_dir      # settings.json, otherwise the auto-detected Steam path


def _global_blob(paks_dir: str) -> bytes:
    raw = open(os.path.join(paks_dir, "global.utoc"), 'rb').read()
    enc = raw[144:] + b"\0" * (-len(raw[144:]) % 16)
    body = raw[:144] + ecb_decrypt(HARDCODED_KEY_DEFAULT, enc)
    nchunks, nblocks = struct.unpack_from('<II', raw, 24)
    p = 144 + 12 * nchunks + 10 * nchunks
    nseeds = struct.unpack_from('<I', raw, 84)[0]
    p += 4 * nseeds
    theia = TheiaDecryptor(open(os.path.join(paks_dir, "global.ucas.meta"), 'rb').read(),
                           os.stat(os.path.join(paks_dir, "global.ucas")).st_size,
                           "arcraiders")
    f = open(os.path.join(paks_dir, "global.ucas"), "rb")
    out = bytearray()
    for i in range(nblocks):
        u64v, u32v = struct.unpack_from("<QI", body, p + 12 * i)
        boff, csz = u64v & ((1 << 40) - 1), (u64v >> 40) & ((1 << 24) - 1)
        f.seek(boff)
        data = bytearray(f.read(align(csz)))
        theia.decrypt_range(data, boff)
        out += data[:csz]
    return bytes(out)


def load_script_map(paks_dir: str = PAKS_DEFAULT, cache: str | None = None) -> dict[int, tuple[str, int]]:
    """Raw FScriptObjectEntry.GlobalIndex u64 -> (bare name, OuterIndex u64).

    Keys keep their type bits (62-63) exactly as stored: package import/export
    maps reference these values verbatim, so callers must not mask either side.
    Keyed only on the GlobalIndex column. Raw blob is cached; the map is
    rebuilt cheaply from it.
    """
    if cache and os.path.exists(cache):
        blob = open(cache, 'rb').read()
    else:
        blob = _global_blob(paks_dir)
        if cache:
            os.makedirs(os.path.dirname(os.path.abspath(cache)), exist_ok=True)
            open(cache, 'wb').write(blob)

    count, strbytes, hashver = struct.unpack_from('<IIQ', blob, 0)
    meta_pos = 16 + 8 * count
    q = meta_pos + 2 * count
    names = []
    for h in struct.unpack_from(f'<{count}H', blob, meta_pos):
        ln = ((h & 0x7F) << 8) | (h >> 8)          # FSerializedNameHeader length
        if h & 0x80:                               # IsWide = bit 7
            names.append(blob[q:q + ln * 2].decode('utf-16-le', 'replace'))
            q += ln * 2
        else:
            names.append(blob[q:q + ln].decode('utf-8', 'replace'))
            q += ln
    base = meta_pos + 2 * count + strbytes
    # sanity: walk must land exactly on the string end
    if abs(q - base) > 8:
        raise ValueError(f"script name walk drift: {q} vs {base}")
    nso, = struct.unpack_from('<I', blob, base)
    # The ScriptImport ids in a package equal FScriptObjectEntry.GlobalIndex
    # BIT FOR BIT, type bits (62-63) included. Never mask them.
    smap: dict[int, tuple[str, int]] = {}
    for i in range(nso):
        p = base + 4 + 32 * i
        mn, = struct.unpack_from('<I', blob, p)          # FMappedName.NameIndex @0
        gi, oi, _cdo = struct.unpack_from('<3Q', blob, p + 8)
        if gi == 0xFFFFFFFFFFFFFFFF:
            continue
        ni = mn & 0x3FFFFFFF
        name = names[ni] if mn != 0xFFFFFFFF and ni < len(names) else ""
        smap[gi] = (name, oi)
    return smap


INVALID = 0xFFFFFFFFFFFFFFFF


def class_path(smap: dict[int, tuple[str, int]], key: int) -> str:
    """Raw ScriptImport u64 (as stored in the import/export map) -> full path.

    Keys are matched unmasked; outer chain is followed through OuterIndex.
    """
    entry = smap.get(key)
    if entry is None:
        return f"script:{key:#018x}"
    name, outer = entry
    chain = [name]
    seen = {key}
    while outer and outer != INVALID and outer in smap and outer not in seen:
        seen.add(outer)
        name, outer = smap[outer]
        if name:
            chain.append(name)
    return ".".join(reversed([c for c in chain if c]))


if __name__ == "__main__":
    import tempfile
    m = load_script_map(cache=os.path.join(tempfile.gettempdir(), "glob.blob"))
    import collections
    print("entries:", len(m))
    print("top:", collections.Counter(m.values()).most_common(8))

def load_global_names(cache: str | None = "cache/globalnames.bin",
                      paks_dir: str = PAKS_DEFAULT) -> list[str]:
    """Index -> name for the GLOBAL name map (FName table dumped in
    global.ucas alongside ScriptObjects).

    Same FSerializedNameHeader encoding as the package batches: the 16-bit
    big-endian header word carries IsWide in bit 15 and the length split across bytes
    (ln = ((h & 0x7F) << 8) | (h >> 8)). Cached as a compact binary list.
    """
    if cache and os.path.exists(cache):
        blob = open(cache, "rb").read()
        out, q = [], 4
        n, = struct.unpack_from("<I", blob, 0)
        for _ in range(n):
            ln, = struct.unpack_from("<H", blob, q); q += 2
            out.append(blob[q:q + ln].decode("utf-8", "replace")); q += ln
        return out

    raw = _global_blob(paks_dir)
    count, strbytes, _hashver = struct.unpack_from("<IIQ", raw, 0)
    meta = 16 + 8 * count                       # after GlobalIndex records
    q = meta + 2 * count                        # skip the header words
    names = []
    for h in struct.unpack_from(f"<{count}H", raw, meta):
        ln = ((h & 0x7F) << 8) | (h >> 8)       # FSerializedNameHeader length
        if h & 0x80:                            # IsWide
            names.append(raw[q:q + ln * 2].decode("utf-16-le", "replace")); q += ln * 2
        else:
            names.append(raw[q:q + ln].decode("utf-8", "replace")); q += ln
    if cache:
        buf = bytearray(struct.pack("<I", len(names)))
        for nm in names:
            b = nm.encode("utf-8")
            buf += struct.pack("<H", len(b)) + b
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        os.makedirs(os.path.dirname(os.path.abspath(cache)), exist_ok=True)
        open(cache, "wb").write(bytes(buf))
    return names


def class_name(smap: dict[int, tuple[str, int]], key: int) -> str | None:
    """Bare script class name for a package index that is a ScriptImport."""
    rec = smap.get(key & ((1 << 62) - 1)) or smap.get(key)
    return rec[0] if rec else None
