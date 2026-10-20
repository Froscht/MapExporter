"""IoStore (.utoc/.ucas) reader and block decompression.

- utoc header; the body is AES-ECB encrypted with a hardcoded key for most containers and
  plain for some (e.g. global.utoc), both are tried
- directory index (mount point + directory/file trees + string table)
- 10-byte packed offset/length chunk table, 12-byte packed block table
- .ucas data blocks: Theia-decrypted, then decompressed by method
"""
import ctypes
import ctypes.util
import struct
import zlib
from pathlib import Path

from Crypto.Cipher import AES

from .crypto import TheiaDecryptor


# ---------- block decompression (oodle / zlib / zstd) ----------

_oodle = None
_oodle_error = None


def load_oodle(lib_path: str | None = None):
    """Load liboodle-decomp-x86_64.so (bundled with the game). Returns True on success."""
    global _oodle, _oodle_error
    if _oodle is not None:
        return True
    candidates = []
    if lib_path:
        candidates.append(Path(lib_path))
    import sys
    if sys.platform == "win32":
        candidates += list(Path(__file__).parent.glob("oodle*/oo2core*.dll"))
        candidates += list(Path(__file__).parent.glob("oodle*/*oodle*.dll"))
    else:
        candidates += list(Path(__file__).parent.glob("oodle*/liboodle*"))
        candidates += list(Path.home().glob(".local/share/Steam/steamapps/common/Arc Raiders/*/liboodle*"))
    for c in candidates:
        if c.exists():
            try:
                lib = ctypes.CDLL(str(c))
                lib.OodleLZ_Decompress.restype = ctypes.c_long
                lib.OodleLZ_Decompress.argtypes = [
                    ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int,
                    ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
                _oodle = lib
                return True
            except OSError as e:
                _oodle_error = str(e)
    return False


def decompress(data: bytes, method: str, expected_size: int) -> bytes:
    method = (method or "None").lower()
    if method in ("none", "raw"):
        return data
    if method == "zlib":
        return zlib.decompress(data)
    if method in ("zstd", "zstandard"):
        # IoStore stores frameless ("raw") zstd blocks
        return _zstd_raw(data, expected_size)
    if method == "oodle":
        if load_oodle():
            out = ctypes.create_string_buffer(expected_size + 64)
            r = _oodle.OodleLZ_Decompress(data, len(data), out, expected_size,
                                          1, 0, 0, None, 0, None, 0, None, 0, 3)
            if r > 0:
                return raw_bytes(out, r)
        raise RuntimeError(f"Ooodle decompression unavailable ({_oodle_error})")
    raise ValueError(f"unknown compression method {method!r}")


# raw zstd via the system libzstd (ctypes)
def _zstd_lib():
    import ctypes
    last = None
    for name in ("libzstd.so.1", "libzstd.dll", "zstd.dll", ctypes.util.find_library("zstd")):
        if not name:
            continue
        try:
            return ctypes.CDLL(name)
        except OSError as e:
            last = e
    raise RuntimeError(f"zstd-Bibliothek nicht gefunden ({last})")


_ctx = None


def zstd_create_ctx():
    global _ctx
    if _ctx is None:
        lib = _zstd_lib()
        lib.ZSTD_createCCtx.restype = ctypes.c_void_p
        lib.ZSTD_decompressCCtx.restype = ctypes.c_size_t
        lib.ZSTD_decompressCCtx.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t,
                                            ctypes.c_char_p, ctypes.c_size_t]
        _ctx = (lib, lib.ZSTD_createCCtx())
    return _ctx


def raw_bytes(buf, n):
    return ctypes.string_at(ctypes.addressof(buf), n)


def _zstd_raw(data: bytes, expected: int) -> bytes:
    lib, cctx = zstd_create_ctx()
    out = ctypes.create_string_buffer(expected)
    r = lib.ZSTD_decompressCCtx(cctx, out, expected, data, len(data))
    if r != expected:
        raise RuntimeError(f"zstd raw decompress returned {r}, expected {expected}")
    return ctypes.string_at(ctypes.addressof(out), r)


# ---------- utoc / ucas container ----------

INVALID = 0xFFFFFFFF
HARDCODED_KEY_DEFAULT = bytes.fromhex("5A4741BC469E10E569D48057B7AB43320388C9748759663BB5D13E201CA2052E")


def align(v, a=16):
    return (v + a - 1) // a * a


def read_fstring(buf, pos):
    n = struct.unpack_from("<i", buf, pos)[0]
    pos += 4
    if n == 0:
        return "", pos
    if n < 0:
        cnt = -n
        s = buf[pos:pos + cnt * 2].decode("utf-16-le", "replace")
        pos += cnt * 2
    else:
        s = buf[pos:pos + n].decode("utf-8", "replace")
        pos += n
    return s.rstrip("\x00"), pos


def ecb_decrypt(key, data):
    return AES.new(key, AES.MODE_ECB).decrypt(data[:len(data) // 16 * 16])


class IoStoreContainer:
    def __init__(self, utoc_path: Path, game: str = "arcraiders", hardcoded_key: bytes = None):
        self.path = utoc_path
        self.base = utoc_path.name[:-5]
        self.ucas_path = utoc_path.with_suffix(".ucas")
        raw = utoc_path.read_bytes()
        (self.version, _r0, _r1, hdrsz, self.nchunks, self.nblocks, self.block_entry_size,
         self.nmethods, self.method_name_length, self.cbsize, self.dir_size,
         self.npartitions, self.container_id) = struct.unpack_from("<BBHIIIIIIIIIQ", raw, 16)
        if self.nchunks <= 0 or self.nblocks <= 0 or self.cbsize <= 0 or hdrsz > len(raw):
            raise ValueError(f"{utoc_path.name}: implausible header")
        if hardcoded_key is None:
            hardcoded_key = HARDCODED_KEY_DEFAULT

        enc = raw[144:]
        enc += b"\0" * (-len(enc) % 16)
        bodies = [raw[:144] + ecb_decrypt(hardcoded_key, enc), raw]
        last_err = None
        for body in bodies:
            try:
                self._parse_body(body)
                break
            except Exception as e:
                last_err = e
        else:
            raise ValueError(f"{utoc_path.name}: cannot parse ({last_err})")

        self._theia = None
        self._ucas = None

    def _parse_body(self, body):
        pos = 144
        self.chunk_ids = [body[pos + 12 * i: pos + 12 * i + 12].hex() for i in range(self.nchunks)]
        pos += 12 * self.nchunks
        self.chunk_offsets = []
        for i in range(self.nchunks):
            b = body[pos + 10 * i: pos + 10 * i + 10]
            self.chunk_offsets.append((int.from_bytes(b[0:5], "big"), int.from_bytes(b[5:10], "big")))
        pos += 10 * self.nchunks
        nseeds = struct.unpack_from("<I", body, 84)[0]
        nnoph = struct.unpack_from("<I", body, 96)[0]
        self.flags = struct.unpack_from("<I", body, 80)[0]
        self.container_guid = struct.unpack_from("<4I", body, 64)
        self.partition_size = struct.unpack_from("<Q", body, 88)[0]
        if self.version >= 4:
            self.seeds = list(struct.unpack_from(f"<{nseeds}i", body, pos))
        pos += 4 * nseeds
        if self.version >= 5:
            self.no_header = list(struct.unpack_from(f"<{nnoph}i", body, pos))
        pos += 4 * nnoph

        self.blocks = []
        maxoff = 0
        for i in range(self.nblocks):
            u64v, u32v = struct.unpack_from("<QI", body, pos + 12 * i)
            boff = u64v & ((1 << 40) - 1)
            csz = (u64v >> 40) & ((1 << 24) - 1)
            usz = u32v & ((1 << 24) - 1)
            method = (u32v >> 24) & 0xFF
            self.blocks.append((boff, csz, usz, method))
            maxoff = max(maxoff, boff + align(csz))
        pos += 12 * self.nblocks
        if self.method_name_length and self.nmethods:
            self.methods = [body[pos + self.method_name_length * i:
                                 pos + self.method_name_length * (i + 1)].split(b"\x00")[0].decode()
                            for i in range(self.nmethods)]
        else:
            self.methods = []
        pos += self.method_name_length * self.nmethods
        if self.flags & 4:  # signature: hash string + per-block hashes
            hs = struct.unpack_from("<i", body, pos)[0]
            if hs < 0 or hs > 4096:
                raise ValueError("bad signature hash size")
            pos += 4 + 2 * hs + 20 * self.nblocks

        self.directoryless = False
        # Containers that end right after the signature section (global.utoc) carry a valid
        # chunk/block table but no mount point / dir / file / string tables. Detect that before
        # parsing the tail, so the container stays mountable instead of failing outright.
        def _plausible_fstring(p):
            if p + 4 > len(body):
                return False
            n = struct.unpack_from("<i", body, p)[0]
            if n == 0:
                return True
            if 0 < n <= 512:
                return all(0x20 <= b < 0x7f or b == 0 for b in body[p + 4:p + 4 + n])
            return -512 <= n < 0
        if not _plausible_fstring(pos) and len(body) - pos <= (1 << 20):
            dsize = self.ucas_path.stat().st_size if self.ucas_path.exists() else None
            if dsize and maxoff > dsize + 16 * 1024 * 1024:
                raise ValueError(f"block offsets ({maxoff}) exceed data file ({dsize})")
            self.mount_point, self.entries, self.directoryless = "", [], True
            return
        self.mount_point, dp = read_fstring(body, pos)
        if len(self.mount_point) > 256:
            raise ValueError("bad mount point")
        ndir = struct.unpack_from("<i", body, dp)[0]
        if ndir < 0 or ndir > 1_000_000:
            raise ValueError("bad dir count")
        dp += 4
        dirs = [struct.unpack_from("<4I", body, dp + 16 * i) for i in range(ndir)]
        dp += 16 * ndir
        nfile = struct.unpack_from("<i", body, dp)[0]
        if nfile < 0 or nfile > 10_000_000:
            raise ValueError("bad file count")
        dp += 4
        files = [struct.unpack_from("<3I", body, dp + 12 * i) for i in range(nfile)]
        dp += 12 * nfile
        strcount = struct.unpack_from("<I", body, dp)[0]
        if strcount > 10_000_000:
            raise ValueError("bad string count")
        sp = dp + 4
        strs = []
        for _ in range(strcount):
            n = struct.unpack_from("<i", body, sp)[0]
            if abs(n) > 8192 or sp + 4 + abs(n) > len(body):
                raise ValueError("string table overrun")
            sp += 4
            if n < 0:
                cnt = -n
                s = body[sp:sp + cnt * 2].decode("utf-16-le", "replace")
                sp += cnt * 2
            else:
                s = body[sp:sp + n].decode("utf-8", "replace")
                sp += n
            strs.append(s.rstrip("\x00"))

        self.entries = []
        stack = [(0, "")]
        while stack:
            di, prefix = stack.pop()
            name_i, child, sibling, firstfile = dirs[di]
            p = prefix
            if name_i != INVALID and name_i < len(strs) and strs[name_i]:
                p = prefix + strs[name_i] + "/"
            if sibling != INVALID:
                stack.append((sibling, prefix))
            if child != INVALID:
                stack.append((child, p))
            f = firstfile
            while f != INVALID:
                fn, nxt, ud = files[f]
                nm = strs[fn] if fn != INVALID and fn < len(strs) else "?"
                self.entries.append((self.mount_point.rstrip("/") + "/" + p + nm, ud))
                f = nxt
        self.entries.sort()
        # physical sanity: block offsets should not exceed co-resident data file
        dsize = self.ucas_path.stat().st_size if self.ucas_path.exists() else None
        if dsize and maxoff > dsize + 16 * 1024 * 1024:
            raise ValueError(f"block offsets ({maxoff}) exceed data file ({dsize})")

    def _decryptor(self):
        if self._theia is None and self.ucas_path.exists():
            meta_path = Path(str(self.ucas_path) + ".meta")
            if meta_path.exists():
                self._theia = TheiaDecryptor(meta_path.read_bytes(),
                                             self.ucas_path.stat().st_size, "arcraiders")
        return self._theia

    def _data(self):
        if self._ucas is None:
            self._ucas = self.ucas_path.open("rb")
        return self._ucas

    def get_chunk(self, entry_index: int) -> bytes:
        off, ln = self.chunk_offsets[entry_index]
        cb = self.cbsize
        first, last = off // cb, (off + ln - 1) // cb
        f = self._data()
        dec = self._decryptor()
        out = bytearray()
        for bi in range(first, last + 1):
            boff, csz, usz, method = self.blocks[bi]
            f.seek(boff)
            data = bytearray(f.read(align(csz)))
            if dec is not None:
                dec.decrypt_range(data, boff)
            method_name = self.methods[method] if method < len(self.methods) else "None"
            plain = decompress(bytes(data[:csz]), method_name, usz)
            skip = off % cb if bi == first else 0
            out += plain[skip:skip + min(len(plain) - skip, ln - len(out))]
        return bytes(out[:ln])

    def close(self):
        if self._ucas is not None:
            self._ucas.close()
            self._ucas = None


def split_rank(utoc_name) -> int:
    """0 = split container (pakchunkN_sM), 1 = its base container.

    Some package paths exist in BOTH the base container and its split container.
    Rule: split containers first, and each path is used only once. Both copies are
    byte-identical, so the order is only a deterministic tie-break, not a correctness issue.
    """
    return 0 if "_s" in Path(utoc_name).name.split("-")[0] else 1


def package_entries(containers, substr: str = "",
                    exts=(".uasset", ".umap")) -> list[tuple]:
    """(container, path, entry_id) for package chunks, once per path, split copy preferred.

    Use this instead of scanning containers in mount order, so every path is
    visited once with a deterministic container choice.
    """
    seen, out = set(), []
    for c in sorted(containers, key=lambda k: split_rank(k.path)):
        for p, ud in c.entries:
            if p.endswith(exts) and substr in p and p not in seen:
                seen.add(p)
                out.append((c, p, ud))
    return out


def mount_directory(pak_dir: str | Path, game: str = "arcraiders") -> list[IoStoreContainer]:
    pak_dir = Path(pak_dir)
    containers = []
    for utoc in sorted(pak_dir.glob("*.utoc")):
        try:
            containers.append(IoStoreContainer(utoc, game))
        except Exception as e:
            print(f"[warn] failed to mount {utoc.name}: {e}")
    containers.sort(key=lambda c: split_rank(c.path))   # split copies before their base
    return containers


# ---------- path index ----------

def path_index(containers, substr: str = "", exts=None) -> dict:
    """{path: [(container, entry_id), …]} over ALL entries (non-packages too), split first.
    Build it once and reuse it — that is the cheap way for scan loops."""
    out: dict[str, list] = {}
    for c in containers:
        rank = split_rank(c.path.name)
        for p, ud in c.entries:
            if substr and substr not in p:
                continue
            if exts and not p.endswith(exts):
                continue
            out.setdefault(p, []).append((c, ud))
    for lst in out.values():
        lst.sort(key=lambda h: split_rank(h[0].path.name))
    return out
