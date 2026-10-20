"""Reader for classic UE .pak files (version 11/12) as used by ARC Raiders.

Layout: the whole file is Theia-encrypted (sidecar <file>.pak.meta, as with .ucas); underneath
is a normal UE5 pak: footer at the end of the file, AES-encrypted index with "encoded entries"
and a full directory index. Mainly needed for the localization files
(Localization/Game/<culture>/Game.locres), which are not stored in IoStore.
"""
from __future__ import annotations

import struct
from pathlib import Path

from Crypto.Cipher import AES

from .crypto import TheiaDecryptor

PAK_MAGIC = 0x5A6F12E1


def _fstring(b, p):
    n = struct.unpack_from("<i", b, p)[0]
    p += 4
    if n == 0:
        return "", p
    if n < 0:
        s = bytes(b[p:p - 2 * n]).decode("utf-16-le", "replace")
        return s.rstrip("\x00"), p - 2 * n
    s = bytes(b[p:p + n]).decode("utf-8", "replace")
    return s.rstrip("\x00"), p + n


class PakFile:
    def __init__(self, path, aes_keys=()):
        self.path = Path(path)
        self.size = self.path.stat().st_size
        meta = Path(str(self.path) + ".meta")
        self._theia = TheiaDecryptor(meta.read_bytes(), self.size) if meta.exists() else None
        self._keys = [bytes.fromhex(k["key"] if isinstance(k, dict) else k) for k in aes_keys]
        self._f = None
        self.files: dict[str, tuple[int, int, bool]] = {}   # path -> (offset, size, encrypted)
        self._read_index()

    # ---- raw access ----
    def _read(self, off, n) -> bytearray:
        if self._f is None:
            self._f = open(self.path, "rb")
        self._f.seek(off)
        data = bytearray(self._f.read(n))
        if self._theia is not None:
            self._theia.decrypt_range(data, off)
        return data

    def _aes(self, data) -> bytes:
        if not self._keys:
            raise ValueError(f"{self.path.name}: Index ist verschlüsselt, aber kein AES-Key gesetzt")
        return AES.new(self._keys[0], AES.MODE_ECB).decrypt(bytes(data[:len(data) // 16 * 16]))

    def _block(self, off, n, encrypted) -> bytes:
        if not encrypted:
            return bytes(self._read(off, n))
        return self._aes(self._read(off, (n + 15) // 16 * 16))[:n]

    # ---- Index ----
    def _read_index(self):
        tail_n = min(self.size, 1024)
        tail = self._read(self.size - tail_n, tail_n)
        i = tail.rfind(struct.pack("<I", PAK_MAGIC))
        if i < 1:
            raise ValueError(f"{self.path.name}: kein Pak-Footer")
        enc_index = tail[i - 1] != 0
        self.version, ioff, isz = struct.unpack_from("<IQQ", tail, i + 4)
        if self.version < 10:
            raise ValueError(f"{self.path.name}: Pak-Version {self.version} nicht unterstützt")
        idx = self._block(ioff, isz, enc_index)
        self.mount, p = _fstring(idx, 0)
        n_entries, _seed = struct.unpack_from("<iQ", idx, p); p += 12
        has_hash = struct.unpack_from("<i", idx, p)[0]; p += 4
        if has_hash:
            p += 8 + 8 + 20
        has_full = struct.unpack_from("<i", idx, p)[0]; p += 4
        if not has_full:
            raise ValueError(f"{self.path.name}: kein vollständiger Verzeichnis-Index")
        doff, dsz = struct.unpack_from("<qq", idx, p); p += 8 + 8 + 20
        enc_size = struct.unpack_from("<i", idx, p)[0]; p += 4
        self._encoded = bytes(idx[p:p + enc_size])
        d = self._block(doff, dsz, enc_index)
        q = 0
        ndirs = struct.unpack_from("<i", d, q)[0]; q += 4
        mount = self.mount.replace("../../../", "")
        for _ in range(ndirs):
            dname, q = _fstring(d, q)
            nf = struct.unpack_from("<i", d, q)[0]; q += 4
            for _ in range(nf):
                fname, q = _fstring(d, q)
                loc = struct.unpack_from("<i", d, q)[0]; q += 4
                if loc >= 0:
                    self.files[(mount + dname.lstrip("/") + fname)] = self._decode_entry(loc)

    def _decode_entry(self, p):
        b = self._encoded
        v = struct.unpack_from("<I", b, p)[0]; p += 4
        if (v & 0x3F) == 0x3F:
            p += 4
        method = (v >> 23) & 0x3F
        if v & (1 << 31):
            off = struct.unpack_from("<I", b, p)[0]; p += 4
        else:
            off = struct.unpack_from("<Q", b, p)[0]; p += 8
        if v & (1 << 30):
            usize = struct.unpack_from("<I", b, p)[0]; p += 4
        else:
            usize = struct.unpack_from("<Q", b, p)[0]; p += 8
        if method:
            raise NotImplementedError("komprimierte Pak-Einträge werden nicht unterstützt")
        encrypted = bool((v >> 22) & 1)
        return off, usize, encrypted

    # ---- files ----
    def find(self, suffix: str) -> list[str]:
        return [k for k in self.files if k.endswith(suffix)]

    def read(self, name: str) -> bytes:
        off, size, encrypted = self.files[name]
        # the data is preceded by the serialized FPakEntry (v12, uncompressed: 53 bytes)
        hdr = 8 + 8 + 8 + 4 + 20 + 1 + 4
        return self._block(off + hdr, size, encrypted)

    def close(self):
        if self._f:
            self._f.close()
            self._f = None


def open_paks(pak_dir, aes_keys, quiet=False) -> list[PakFile]:
    out = []
    for p in sorted(Path(pak_dir).glob("*.pak")):
        try:
            out.append(PakFile(p, aes_keys))
        except Exception as e:  # empty paks (365 B) often have no directory index
            if not quiet:
                print(f"[warn] {p.name}: {e}")
    return out
