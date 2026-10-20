"""Container encryption: Blake3 compression function and the Theia keystream cipher.

ARC Raiders encrypts .ucas / .pak containers with a Blake3-based keystream that is subtracted
byte by byte. Page keys live in the <file>.meta sidecar (magic 'metadat0'). A small C kernel
(theia.c -> theia_kern.so / theia_kern.dll) does the hot loop; a pure-Python fallback exists.

The Theia decryption is a port of CUE4Parse's TheiaDecryptor (Apache-2.0), see NOTICE.
"""
import ctypes
import math
import os
import struct
import subprocess
import sys
from pathlib import Path


# ---------- Blake3 ----------
IV = [0x6A09E667, 0xBB67AE85, 0x3C6EF372, 0xA54FF53A, 0x510E527F, 0x9B05688C, 0x1F83D9AB, 0x5BE0CD19]
MSG_PERMUTATION = [2, 6, 3, 10, 7, 0, 4, 13, 1, 11, 12, 5, 9, 14, 15, 8]
CHUNK_START, CHUNK_END, PARENT, ROOT = 1 << 0, 1 << 1, 1 << 2, 1 << 3
M32 = 0xFFFFFFFF


def _r32(v):
    return v & M32


def _ror(x, n):
    return (x >> n) | (x << (32 - n)) & M32


def compress(chaining, block_words, counter, block_len, flags):
    """Blake3 compression function. chaining: 8 u32, block_words: 16 u32 -> 16 u32."""
    st = list(chaining) + IV[:4] + [counter & M32, (counter >> 32) & M32, block_len & M32, flags & M32]
    m = list(block_words)

    def g(a, b, c, d, mx, my):
        st[a] = _r32(st[a] + st[b] + mx)
        st[d] = _ror(st[d] ^ st[a], 16)
        st[c] = _r32(st[c] + st[d])
        st[b] = _ror(st[b] ^ st[c], 12)
        st[a] = _r32(st[a] + st[b] + my)
        st[d] = _ror(st[d] ^ st[a], 8)
        st[c] = _r32(st[c] + st[d])
        st[b] = _ror(st[b] ^ st[c], 7)

    for rnd in range(7):
        g(0, 4, 8, 12, m[0], m[1]); g(1, 5, 9, 13, m[2], m[3])
        g(2, 6, 10, 14, m[4], m[5]); g(3, 7, 11, 15, m[6], m[7])
        g(0, 5, 10, 15, m[8], m[9]); g(1, 6, 11, 12, m[10], m[11])
        g(2, 7, 8, 13, m[12], m[13]); g(3, 4, 9, 14, m[14], m[15])
        if rnd < 6:
            m = [m[MSG_PERMUTATION[i]] for i in range(16)]

    out = [0] * 16
    for i in range(8):
        out[i] = _r32(st[i] ^ st[i + 8])
        out[i + 8] = _r32(st[i + 8] ^ chaining[i])
    return out


def blake3_hash(data: bytes) -> bytes:
    """Standard unkeyed Blake3 digest (32 bytes). Handles single-chunk inputs <= 1024 bytes
    (all uses in Theia are 64-byte inputs)."""
    if len(data) > 64:
        raise ValueError("this helper only supports inputs <= 64 bytes")
    block = bytearray(64)
    n = min(len(data), 64)
    block[:n] = data[:n]
    words = list(struct.unpack("<16I", bytes(block)))
    # single-chunk finalize: ONE compression with CHUNK_START|CHUNK_END|ROOT
    out = compress(IV, words, 0, n, CHUNK_START | CHUNK_END | ROOT)
    return struct.pack("<8I", *out[:8])


def words_from_bytes(b: bytes):
    return list(struct.unpack(f"<{len(b) // 4}I", b))


# ---------- Theia keystream ----------
PAGE, BLOCK = 0x10000, 64
META_MAGIC = b"metadat0"
ARCHIVE_SIZE_OFFSET = 80
HEADER_SIZE, KEY_OFFSET, RECORD_SIZE = 512, 32, 64

GAME_KEYS = {
    "arcraiders": [0xBEDC8A41, 0xC2083AA2, 0x7A40B8E0, 0x07D37C60, 0x16B42333, 0x5F76DE9B, 0x53881F06, 0x107B149C],
    "marveltokon": [0x99899BE5, 0x3958611C, 0x64BC2227, 0xDDD76F19, 0x356B9B15, 0x3841E7E8, 0xB52A7A85, 0xFA43C8E5],
    "highguard": [0x63490CBF, 0x955C29C9, 0xB8AE5CB3, 0xF6C18D04, 0xA0C7C061, 0xA1F27398, 0xF52305B0, 0x7E12F14A],
}


def _load_kernel():
    """Try to load the compiled keystream kernel; compile it if missing."""
    here = Path(__file__).parent
    win = sys.platform == "win32"
    so = here / ("theia_kern.dll" if win else "theia_kern.so")
    src = here / "theia.c"
    if not so.exists() and src.exists():
        cmd = (["gcc", "-O2", "-shared", "-o", str(so), str(src)] if win else
               ["cc", "-O2", "-shared", "-fPIC", "-o", str(so), str(src)])
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except Exception:
            return None
    if so.exists():
        try:
            lib = ctypes.CDLL(str(so))
            fn = lib.theia_decrypt_page
            fn.restype = None
            fn.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_uint32),
                           ctypes.c_uint32, ctypes.c_int, ctypes.c_int]
            return fn
        except Exception:
            return None
    return None


_KERNEL = _load_kernel()


class TheiaDecryptor:
    def __init__(self, meta: bytes, file_size: int, game: str = "arcraiders"):
        if len(meta) < 8 or meta[:8] != META_MAGIC:
            raise ValueError("Invalid Theia metadata: missing 'metadat0' magic")
        self.const_key = GAME_KEYS[game]
        if struct.unpack_from("<q", meta, ARCHIVE_SIZE_OFFSET)[0] != file_size:
            raise ValueError("Invalid Theia metadata: file size mismatch")
        raw_master = meta[ARCHIVE_SIZE_OFFSET + 16: ARCHIVE_SIZE_OFFSET + 48]  # 32 bytes
        hash_block = bytes(raw_master) + bytes(32)
        self.master_key = words_from_bytes(blake3_hash(hash_block))  # 8 words

        pages = (file_size + PAGE - 1) // PAGE
        if len(meta) < HEADER_SIZE + pages * RECORD_SIZE:
            raise ValueError(f"Theia metadata too small for {pages} pages")
        self.page_states = {}
        self._page_keys = memoryview(meta)
        self.file_size = file_size

    def _page_state(self, page: int):
        st = self.page_states.get(page)
        if st is not None:
            return st
        off = HEADER_SIZE + page * RECORD_SIZE + KEY_OFFSET
        pk = self._page_keys[off:off + 32]
        arg2 = list(self.master_key) + words_from_bytes(bytes(pk))
        digest = compress(self.const_key, arg2, 0, BLOCK, 0x11)
        state_words = digest[:8]           # first 32 bytes of digest, as 8 LE words
        self.page_states[page] = (state_words, 1)  # sb = state33[32] = 0x01
        return self.page_states[page]

    def _keystream_block(self, state_words, sb, block_index):
        return compress(state_words, [0] * 16, block_index, sb, sb ^ 0x1B)

    def decrypt_range(self, data: bytearray, file_offset: int):
        """In-place decryption of data read at file_offset.

        The keystream block counter is anchored to the FILE position within its 64 KiB page,
        not to the buffer position: page = key, (file_offset % PAGE) // BLOCK = counter.
        A container block may start in the middle of a page and in the middle of a keystream
        block, so the kernel also gets the byte offset inside the first keystream block (skip)."""
        if not data:
            return data
        first = file_offset // PAGE
        last = (file_offset + len(data) - 1) // PAGE
        if _KERNEL is not None:
            for page in range(first, last + 1):
                pstart = page * PAGE
                s = max(file_offset, pstart)
                e = min(file_offset + len(data), pstart + PAGE)
                if e <= s:
                    continue
                state_words, sb = self._page_state(page)
                arr = (ctypes.c_uint32 * 8)(*state_words)   # parentheses required, else 8(*words)
                buf = (ctypes.c_char * (e - s)).from_buffer(data, s - file_offset)
                _KERNEL(buf, e - s, arr, sb, (s - pstart) // BLOCK, (s - pstart) % BLOCK)
            return data
        # pure-python fallback (same algorithm as theia.c)
        for page in range(first, last + 1):
            pstart = page * PAGE
            s = max(file_offset, pstart)
            e = min(file_offset + len(data), pstart + PAGE)
            if e <= s:
                continue
            state_words, sb = self._page_state(page)
            ip = s - pstart                      # page offset of the range start
            n = e - s
            buf_off = s - file_offset
            for blk in range(ip // BLOCK, (ip + n + BLOCK - 1) // BLOCK):
                ks = self._keystream_block(state_words, sb, blk)
                kb = struct.pack("<16I", *ks)
                lo = max(0, blk * BLOCK - ip)
                hi = min(n, blk * BLOCK + BLOCK - ip)
                for i in range(lo, hi):
                    data[buf_off + i] = (data[buf_off + i] - kb[ip + i - blk * BLOCK]) & 0xFF
        return data
