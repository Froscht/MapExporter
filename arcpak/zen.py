"""Zen (UE5 IoStore) package parser and the unversioned property header."""
from __future__ import annotations

import struct


# ---------- Zen package ----------

EXPORT_STRIDE = 72

def _u32(b, p):  return struct.unpack_from('<I', b, p)[0]
def _i32(b, p):  return struct.unpack_from('<i', b, p)[0]
def _u64(b, p):  return struct.unpack_from('<Q', b, p)[0]

# FMappedName (u32 Number): bits 0-29 = index into a name map, bits 30-31 = Type
#   0 = package-local name map, 1 = global name map, 2 = container-local name map
NAME_INDEX_MASK = 0x3FFFFFFF
NAME_TYPE_MASK = 0x3

class ZenExport:
    __slots__ = ("index", "offset", "size", "name", "name_mapped", "outer",
                 "class_idx", "super_idx", "template_idx", "global_import",
                 "public_hash", "object_flags", "filter_flags")

class ZenPackage:
    def __init__(self, data: bytes, global_names=None, container_names=None):
        self.data = data
        self.global_names = global_names
        self.container_names = container_names
        self.unresolved_names = []
        # FMappedName is 8 bytes: Number u32 + ComparisonIndex u32
        (self.b_has_versioning, self.header_size, self.pkg_name_mapped,
         self.pkg_name_cmp, self.package_flags, self.cooked_header_size,
         self.hash_off, self.import_off, self.export_off,
         self.export_bundle_off) = struct.unpack_from('<IIIIIIiiii', data, 0)
        self.dep_bundle_headers_off, self.dep_bundle_entries_off, \
            self.imported_pkg_names_off = struct.unpack_from('<3i', data, 40)
        # FZenPackageCellOffsets (VERSE_CELLS): cell import/export map offsets
        self.cell_import_off, self.cell_export_off = struct.unpack_from('<2i', data, 52)

        p = 60
        self.zen_version = self.ue4_version = self.ue5_version = 0
        if self.b_has_versioning:
            self.zen_version, ue4v, ue5v = struct.unpack_from('<3I', data, p)
            self.ue4_version, self.ue5_version = ue4v, ue5v
            p += 12
            p += 4  # licensee
            # custom version container size varies; locate name batch by probing

        self.name_count, p = self._find_name_batch(data, p)
        self.names = self._read_names(data, p, self.name_count)
        self.pkg_name = self._name(self.pkg_name_mapped, "package")

        n_hash = (self.import_off - self.hash_off) // 8
        self.import_hashes = [_u64(data, self.hash_off + 8 * i) for i in range(n_hash)]

        self.imports = [_u64(data, self.import_off + 8 * i)
                        for i in range((self.export_off - self.import_off) // 8)]
        self.exports = self._read_exports()

    # ------------------------------------------------------------------
    # ---- FName batch ----------------------------------------------------
    # FSerializedNameHeader: the u16 is (data0, data1) little endian:
    #   IsWide = data0 & 0x80  -> BIT 7 of the u16, not bit 15
    #   Length = (data0 & 0x7F) << 8 | data1
    # Wide names consume 2*length bytes; the bulk-batch overload Zen uses adds
    # NO alignment padding.
    @staticmethod
    def _head(h):
        return ((h & 0x7F) << 8) | (h >> 8), bool(h & 0x80)

    def _find_name_batch(self, data, p_start):
        """Locate the inline name batch and validate it EXACTLY: the string
        walk must consume exactly strbytes and end exactly at self.hash_off
        (hashes sit before the headers, so the walk end is checked against the
        next known section instead)."""
        limit = self.import_off if self.import_off > p_start else len(data)
        for p in range(p_start, min(p_start + 64, limit)):
            count, strbytes, hashver = struct.unpack_from('<IIQ', data, p)
            if not (1 <= count < 1_000_000) or not (1 <= strbytes <= 20_000_000):
                continue
            hs = p + 16 + 8 * count                 # per-name u64 hashes first
            if hs + 2 * count > limit:
                continue
            heads = struct.unpack_from(f'<{count}H', data, hs)
            pos, walked = hs + 2 * count, 0
            ok = True
            for h in heads:
                ln, wide = self._head(h)
                if ln == 0 or ln > 65535:
                    ok = False; break
                step = ln * 2 if wide else ln
                if pos + step > limit:
                    ok = False; break
                pos += step
                walked += step      # numStringBytes counts BYTES, not characters: wide
                                    # names (e.g. 20 chars = 40 bytes) would fail otherwise
            if ok and walked == strbytes:
                self._name_batch_pos, self._heads_pos, self._str_end = p, hs, pos
                self.hashver = hashver
                return count, hs
        raise ValueError("name batch not found in zen header")

    def _read_names(self, data, p, count):
        """p is the START OF THE HEAD ARRAY (u16 per name); the name bodies follow
        the whole array, not each individual head. Reading the bodies from p itself
        walks through the head array and shifts every later name by its own length."""
        heads = struct.unpack_from(f'<{count}H', data, p)
        pos = p + 2 * count
        names = []
        for h in heads:
            ln, wide = self._head(h)
            if wide:
                names.append(data[pos:pos + ln * 2].decode('utf-16-le', 'replace'))
                pos += ln * 2
            else:
                names.append(data[pos:pos + ln].decode('utf-8', 'replace'))
                pos += ln
        return names

    # ---- name resolution ------------------------------------------------
    def resolve_name(self, mapped: int):
        """u32 FMappedName.Number -> str. Type bits (30-31) select the name map;
        out-of-range indices fall back through the other maps before giving up."""
        idx, typ = mapped & NAME_INDEX_MASK, (mapped >> 30) & NAME_TYPE_MASK
        order = [{0: "pkg", 1: "global", 2: "container"}.get(typ, "pkg")]
        order += [s for s in ("pkg", "global", "container") if s not in order]
        for src in order:
            table = {"pkg": self.names, "global": self.global_names,
                     "container": self.container_names}[src]
            if table is not None and 0 <= idx < len(table):
                return table[idx]
        return None

    def _name(self, mapped: int, ctx="") -> str:
        s = self.resolve_name(mapped)
        if s is None:
            self.unresolved_names.append((ctx, mapped))
            return f"<name:{mapped:#010x}>"
        return s

    def _read_exports(self):
        count = (self.export_bundle_off - self.export_off) // EXPORT_STRIDE
        out = []
        for i in range(count):
            base = self.export_off + EXPORT_STRIDE * i
            e = ZenExport()
            e.index = i
            e.offset, e.size = _u64(self.data, base), _u64(self.data, base + 8)
            e.name_mapped = _u32(self.data, base + 16)
            e.name = self._name(e.name_mapped, f"export[{i}]")
            e.outer, e.class_idx, e.super_idx, e.template_idx = struct.unpack_from(
                '<4Q', self.data, base + 24)
            e.global_import = ~0 & 0xFFFFFFFFFFFFFFFF   # always invalid in UE5+ cooks
            e.public_hash = _u64(self.data, base + 56)
            e.object_flags = _u32(self.data, base + 64)
            e.filter_flags = self.data[base + 68] if base + 68 < len(self.data) else 0
            out.append(e)
        return out

    # ---- helpers --------------------------------------------------------
    @staticmethod
    def idx_type(v):
        if v == ~0 & 0xFFFFFFFFFFFFFFFF: return "invalid"
        t = v >> 62
        return {0: "export", 1: "script_import", 2: "package_import", 3: "invalid"}[t]

    @staticmethod
    def idx_id(v):  return v & ((1 << 62) - 1)

    def export_class(self, e, script_names=None):
        """Class name for an export. ScriptImport ids are matched against the
        ScriptObjects GlobalIndex column VERBATIM (type bits included)."""
        t = self.idx_type(e.class_idx)
        if t == "export":
            i = self.idx_id(e.class_idx)
            if i < len(self.exports):
                return self.exports[i].name
            return None            # cooked class_idx out of range (native/metadata)
        if t == "script_import" and script_names is not None:
            entry = script_names.get(e.class_idx)          # raw u64 key
            if entry is not None:
                return entry[0] if isinstance(entry, tuple) else entry
            return f"script:{e.class_idx:#018x}"
        return f"{t}:{self.idx_id(e.class_idx) if t != 'invalid' else ''}"

    def bulk_data_map(self):
        """[(serial_offset, duplicate_offset, serial_size, flags, cooked_index)] or None.

        UE5 DATA_RESOURCES bulk data map: directly after the name batch comes a u64 pad
        length + pad, then an i64 map size, then 32-byte entries (u64 SerialOffset,
        u64 DuplicateSerialOffset, u64 SerialSize, u32 Flags, u8 CookedIndex, 3 bytes pad).
        Strict: the map must end EXACTLY at ImportedPublicExportHashesOffset, otherwise
        None — no guessing about a layout that does not close.
        """
        p = self._str_end
        if p + 16 > self.hash_off:
            return None
        pad = _u64(self.data, p)
        p += 8 + pad
        if p + 8 > self.hash_off:
            return None
        size = struct.unpack_from('<q', self.data, p)[0]
        p += 8
        if size < 0 or size % 32 or p + size != self.hash_off:
            return None
        return [struct.unpack_from('<QQQIB', self.data, p + 32 * i) for i in range(size // 32)]

    def imported_package_names(self):
        """[str] — paths of the imported packages (UE5.3+), or None.

        At ImportedPackageNamesOffset: a name batch (u32 count, u32 string bytes, u64 hash
        version, hashes, u16 heads, strings), followed by one u32 FName number per entry.
        Strict: must end EXACTLY at header_size, otherwise None.
        """
        if getattr(self, "_ipn", 0) != 0:
            return self._ipn
        self._ipn = None
        d, p = self.data, self.imported_pkg_names_off
        if not 0 < p <= self.header_size - 4:
            return None
        n = _u32(d, p)
        if n == 0:
            self._ipn = [] if p + 4 == self.header_size else None
            return self._ipn
        strbytes = _u32(d, p + 4)
        q = p + 16 + 8 * n
        heads = struct.unpack_from(f"<{n}H", d, q)
        q += 2 * n
        start, out = q, []
        for h in heads:
            ln, wide = self._head(h)
            if wide:
                out.append(d[q:q + 2 * ln].decode("utf-16-le", "replace")); q += 2 * ln
            else:
                out.append(d[q:q + ln].decode("latin-1")); q += ln
        if q - start != strbytes or q + 4 * n != self.header_size:
            return None
        # the 4 bytes per entry are the FName number: Number > 0 → "<Name>_<Number-1>"
        # (e.g. BP_RSR_CeilingLight_02 + 2 → BP_RSR_CeilingLight_02_1)
        nums = struct.unpack_from(f"<{n}I", d, q)
        out = [nm if k == 0 else f"{nm}_{k - 1}" for nm, k in zip(out, nums)]
        self._ipn = out
        return out

    def resolve_package_import(self, v):
        """FPackageObjectIndex (type PackageImport) -> (package path, PublicExportHash) or None."""
        if v >> 62 != 2:
            return None
        names = self.imported_package_names()
        pi, hi = (v & ((1 << 62) - 1)) >> 32, v & 0xFFFFFFFF
        if names is None or pi >= len(names) or hi >= len(self.import_hashes):
            return None
        return names[pi], self.import_hashes[hi]

    def cooked_export(self, e):
        return self.data[e.offset:e.offset + e.size]


def parse_uasset(data: bytes, global_names=None, container_names=None) -> ZenPackage:
    return ZenPackage(data, global_names, container_names)


# ---------- unversioned property header ----------

class UnversionedError(Exception):
    pass


class UnversionedHeader:
    __slots__ = ("pos", "fragments", "zero_mask", "zero_mask_num", "unmasked_num",
                 "has_non_zero", "has_values")

    def __init__(self, data: bytes, pos: int):
        self.fragments = []
        while True:
            f, = struct.unpack_from("<H", data, pos)
            pos += 2
            self.fragments.append({
                "skip": f & 0x7F,
                "has_zero": bool(f & 0x80),
                "is_last": bool(f & 0x100),
                "value_num": f >> 9,
            })
            if f & 0x100:
                break

        # only values of HasAnyZeroes fragments are tracked in the zero mask
        self.zero_mask_num = sum(fr["value_num"] for fr in self.fragments if fr["has_zero"])
        self.unmasked_num = sum(fr["value_num"] for fr in self.fragments if not fr["has_zero"])

        self.zero_mask, pos = _read_mask(data, pos, self.zero_mask_num)
        self.has_non_zero = self.unmasked_num > 0 or any(b == 0 for b in self.zero_mask)
        self.has_values = self.has_non_zero or self.zero_mask_num > 0
        self.pos = pos

    # convenience for reporting
    @property
    def header_size(self) -> int:
        mask_bytes = 0
        if self.zero_mask_num:
            mask_bytes = 1 if self.zero_mask_num <= 8 else 2 if self.zero_mask_num <= 16 \
                else 4 * ((self.zero_mask_num + 31) // 32)
        return 2 * len(self.fragments) + mask_bytes


def _read_mask(data: bytes, pos: int, bits: int) -> tuple[list[int], int]:
    """Load a bit mask, LSB-first inside every byte, truncated to `bits` bits."""
    if bits <= 0:
        return [], pos
    if bits <= 8:
        raw = data[pos:pos + 1]; pos += 1
    elif bits <= 16:
        raw = data[pos:pos + 2]; pos += 2
    else:
        nwords = (bits + 31) // 32
        raw = data[pos:pos + 4 * nwords]; pos += 4 * nwords
    if len(raw) < (bits + 7) // 8:
        raise UnversionedError("mask runs past the end of the export")
    return [(raw[i >> 3] >> (i & 7)) & 1 for i in range(bits)], pos


class FIterator:
    """Yields (schema_index, is_non_zero) for every serialized property, in schema order."""

    def __init__(self, header: UnversionedHeader):
        self.h = header
        self.frag_i = 0
        self.zero_i = 0
        self.remaining = 0
        self.schema = 0
        if header.has_values:
            self._skip()

    def _cur(self):
        return self.h.fragments[self.frag_i]

    def _skip(self):
        self.schema += self._cur()["skip"]
        while self._cur()["value_num"] == 0:
            if self._cur()["is_last"]:
                raise UnversionedError("last fragment reached inside Skip()")
            self.frag_i += 1
            self.schema += self._cur()["skip"]
        self.remaining = self._cur()["value_num"]

    def _is_non_zero(self) -> bool:
        cur = self._cur()
        return (not cur["has_zero"]) or self.h.zero_mask[self.zero_i] == 0

    def _move_next(self) -> bool:
        self.schema += 1
        self.remaining -= 1
        if self._cur()["has_zero"]:
            self.zero_i += 1
        if self.remaining == 0:
            if self._cur()["is_last"]:
                return False
            self.frag_i += 1
            self._skip()
        return True

    def __iter__(self):
        """Iteration is `do { use current } while (move_next())`: the CURRENT
        slot is consumed first; _move_next only advances. Yielding before moving
        is mandatory, otherwise the header's first value is silently dropped."""
        if not self.h.has_values:
            return
        while True:
            yield self.schema, self._is_non_zero()
            if not self._move_next():
                return

    def count_values(self) -> int:
        return sum(1 for _ in self)


def raw_header(data: bytes, pos: int):
    reverse, raw_props, tail = struct.unpack_from("<iii", data, pos)
    return {"reverse": reverse, "raw_props": raw_props, "tail": tail, "pos": pos + 12}
