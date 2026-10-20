"""arcpak.usmap — native .usmap property-mappings reader.

Supports two layouts:
  * ARC FrostSDKDumper native variant (as emitted by FrostSDKDumper/include/usmap_writer.h):
      magic u16(0x30C4) | version u8 | comp u8(0) | csize u32 | dsize u32
      nameCount u32 | names{u8 len, utf8}
      enumCount u32 | enums{i32 nameIdx, u8 count, i32*count}
      structCount u32 | structs{i32 name, i32 super|-1, u16 propCnt, u16 serCnt,
                   props{u16 idx, u8 dim, i32 nameIdx, type...}}
  * Standard usmap v1..v5 (header has hasVersioning bool; names u8/u16
    depending on version; LargeEnums u16 counts; ExplicitEnumValues explicit u64).
Property type payloads (recursive):
  [t] | [8,inner] | [24,k,v] | [25,inner] | [9,i32 structName] | [26,underlying,i32 enumName]
"""
from __future__ import annotations
import struct
from dataclasses import dataclass, field

MAGIC = 0x30C4

PROP_NAMES = {
    0: "ByteProperty", 1: "BoolProperty", 2: "IntProperty", 3: "FloatProperty",
    4: "ObjectProperty", 5: "NameProperty", 6: "DelegateProperty", 7: "DoubleProperty",
    8: "ArrayProperty", 9: "StructProperty", 10: "StrProperty", 11: "TextProperty",
    12: "InterfaceProperty", 13: "MulticastDelegateProperty", 14: "WeakObjectProperty",
    15: "LazyObjectProperty", 16: "AssetObjectProperty", 17: "SoftObjectProperty",
    18: "UInt64Property", 19: "UInt32Property", 20: "UInt16Property", 21: "Int64Property",
    22: "Int16Property", 23: "Int8Property", 24: "MapProperty", 25: "SetProperty",
    26: "EnumProperty", 27: "FieldPathProperty", 28: "SoftClassProperty",
    30: "OptionalProperty",
}
# Standard usmap EPropertyType: 28 Optional (carries inner), 29 Utf8Str, 30 AnsiStr.
# The table above is the old FrostSDKDumper native variant (28 SoftClass, 30 Optional).
STD_PROP_NAMES = {**PROP_NAMES, 28: "OptionalProperty", 29: "Utf8StrProperty",
                  30: "AnsiStrProperty"}


@dataclass
class PropType:
    type: str                      # e.g. "ArrayProperty"
    struct: str | None = None      # StructProperty struct type name
    inner: "PropType | None" = None    # Array/Set/Optional inner, Map key
    value: "PropType | None" = None    # Map value
    enum: str | None = None        # EnumProperty enum name


@dataclass
class Property:
    index: int
    name: str
    array_dim: int
    type: PropType


@dataclass
class Struct:
    name: str
    super: str | None
    property_count: int            # total including inherited (own-only maps below)
    properties: dict = field(default_factory=dict)   # schema index -> Property
    def lookup(self, name: str):
        for p in self.properties.values():
            if p.name == name:
                return p
        return None


class Usmap:
    def __init__(self):
        self.version = 0
        self.structs: dict[str, Struct] = {}
        self.enums: dict[str, dict[int, str]] = {}
        self.native = False
        self.std_ver = -1                  # layout actually resolved by _try_body

    # ---- low level helpers -------------------------------------------------
    @staticmethod
    def _rd(fmt, b, p):
        v = struct.unpack_from(fmt, b, p)
        return (v[0] if len(v) == 1 else v), p + struct.calcsize(fmt)

    def _parse_type(self, b, p, nameLut, std_ver, opt_inner=True):
        t, p = self._rd('<B', b, p)
        names = PROP_NAMES if self.native else STD_PROP_NAMES
        pt = PropType(names.get(t, f"Type{t}"))
        opt_code = 30 if self.native else 28
        # OptionalProperty: standard usmap writes an inner type, our own dumper
        # (usmap_writer.h WriteOneType) emits a bare type byte for it -> the layout
        # candidate decides, see _try_body.
        if t == 8 or t == 25 or (t == opt_code and opt_inner):
            pt.inner, p = self._parse_type(b, p, nameLut, std_ver, opt_inner)
        elif t == 24:
            pt.inner, p = self._parse_type(b, p, nameLut, std_ver, opt_inner)
            pt.value, p = self._parse_type(b, p, nameLut, std_ver, opt_inner)
        elif t == 9:
            idx, p = self._rd('<i', b, p)
            pt.struct = nameLut[idx] if 0 <= idx < len(nameLut) else None
        elif t == 26:
            if std_ver:
                pt.inner, p = self._parse_type(b, p, nameLut, std_ver, opt_inner)
            else:
                _, p = self._rd('<B', b, p)          # dumper: raw underlying byte
            idx, p = self._rd('<i', b, p)
            pt.enum = nameLut[idx] if 0 <= idx < len(nameLut) else None
        return pt, p

    def _try_body(self, body: bytes, declared_ver: int) -> int:
        """Parse the body, auto-detecting the layout the WRITER actually used.

        Standard readers key two widths off the file version:
          name length  : u16 if version >= LongFName(2) else u8
          enum entries : u16 if version >= LargeEnums(3) else u8
        Our own dumper emitted v3 headers with a u8 body, so the declared
        version cannot be trusted: try it first, then the alternatives, and
        accept only a parse that consumes the body exactly.
        """
        cands = [declared_ver, 3, 2, 1, 0]
        seen = set()
        last_err = None
        for cv in cands:
            for opt_inner in (True, False):
              if (cv, opt_inner) in seen:
                continue
              seen.add((cv, opt_inner))
              self.structs, self.enums = {}, {}
              try:
                end = self._parse_body(body, 0, cv, opt_inner)
              except Exception as e:                       # layout mismatch
                last_err = e
                continue
              if end == len(body) and self.structs:
                self.std_ver = cv
                self.opt_inner = opt_inner
                return cv
              last_err = ValueError(f"layout v{cv} opt_inner={opt_inner}: consumed {end} of {len(body)}")
        raise ValueError(f"usmap body layout not resolvable ({last_err})")

    def _parse_body(self, b, p, std_ver, opt_inner=True):
        rd_name_idx = '<i'
        name_len_fmt = '<H' if std_ver >= 2 else '<B'
        enum_cnt_fmt = '<H' if std_ver >= 3 else '<B'

        nameCount, p = self._rd('<I', b, p)
        nameLut = []
        for _ in range(nameCount):
            ln, p = self._rd(name_len_fmt, b, p)
            nameLut.append(b[p:p + ln].decode('utf-8', 'replace'))
            p += ln

        enumCount, p = self._rd('<I', b, p)
        for _ in range(enumCount):
            ni, p = self._rd(rd_name_idx, b, p)
            cnt, p = self._rd(enum_cnt_fmt, b, p)
            enum = {}
            if std_ver >= 4:                          # explicit values
                for j in range(cnt):
                    val, p = self._rd('<Q', b, p)
                    ei, p = self._rd(rd_name_idx, b, p)
                    enum[val] = nameLut[ei] if 0 <= ei < len(nameLut) else "?"
            else:
                for j in range(cnt):
                    ei, p = self._rd(rd_name_idx, b, p)
                    enum[j] = nameLut[ei] if 0 <= ei < len(nameLut) else "?"
            self.enums.setdefault(nameLut[ni], enum)

        structCount, p = self._rd('<I', b, p)
        for _ in range(structCount):
            ni, p = self._rd(rd_name_idx, b, p)
            si, p = self._rd(rd_name_idx, b, p)
            propCnt, p = self._rd('<H', b, p)
            serCnt, p = self._rd('<H', b, p)
            s = Struct(nameLut[ni], nameLut[si] if si >= 0 else None, propCnt)
            for _ in range(serCnt):
                idx, p = self._rd('<H', b, p)
                dim, p = self._rd('<B', b, p)
                pname, p = self._rd('<i', b, p)
                pt, p = self._parse_type(b, p, nameLut, std_ver, opt_inner)
                for j in range(max(dim, 1)):           # static arrays occupy dim indices
                    s.properties[idx + j] = Property(idx + j, nameLut[pname], max(dim, 1), pt)
            self.structs[s.name] = s
        return p

    # ---- entry points -------------------------------------------------------
    def load(self, data: bytes):
        magic, ver = struct.unpack_from('<HB', data, 0)
        if magic != MAGIC:
            raise ValueError(f"bad usmap magic {magic:#06x}")

        # peek: dumper header has comp byte at offset 3 == 0 and version 3 with
        # u8 name lengths; standard v3+ has hasVersioning bool at offset 3.
        # Discriminate: dumper = version 3 & byte3==0 & byte[12:] nameCount sane.
        csize_at4, dsize_at8 = struct.unpack_from('<II', data, 4)
        is_dumper = (csize_at4 == dsize_at8 and 12 + csize_at4 == len(data))
        if is_dumper:
            self.native = True
            csize, dsize = struct.unpack_from('<II', data, 4)
            p = 12
            if csize != dsize:
                raise ValueError("compressed dumper usmap unsupported")
            self._try_body(data[p:p + dsize], ver)
        else:
            # Header layouts differ between writers. Two observed in the wild:
            #   magic u16 | version u16 | flags u16 | csize u32 | dsize u32 | body   (@14)
            #   magic u16 | version u8  | hasVersioning u8 | comp u8 | csize | dsize  (@8)
            # Locate it self-referentially instead of guessing: an uncompressed
            # body must satisfy csize == dsize == len(data) - body_start.
            comp, csize, dsize, p = 0, 0, 0, 14
            for cand in (4, 5, 6, 8, 10, 14):
                c, d = struct.unpack_from('<II', data, cand)
                if c == d and c > 0 and cand + 8 + c == len(data):
                    comp, csize, dsize, p = data[cand - 1] if cand >= 1 else 0, c, d, cand + 8
                    break
            else:
                # compressed: sizes are equal to each other but not to what remains
                for cand in (4, 5, 6, 8, 10, 14):
                    c, d = struct.unpack_from('<II', data, cand)
                    if 0 < c <= len(data) - cand - 8 and 0 < d <= 64_000_000:
                        comp, csize, dsize, p = data[cand - 1], c, d, cand + 8
                        break
                else:
                    raise ValueError("usmap header: sizes not located")
            body = data[p:p + csize]
            if comp != 0:
                from .iostore import decompress
                method = {1: "Oodle", 2: "Brotli", 3: "ZStandard"}.get(comp)
                body = decompress(body, method, dsize)
            self._try_body(body, ver)
        return self


def load_usmap(path: str) -> Usmap:
    with open(path, 'rb') as f:
        return Usmap().load(f.read())

def _own_props(st):
    """Collapse the dim-expanded index map back to [(Property, dim)] in order."""
    out, seen = [], set()
    for i in sorted(st.properties):
        p = st.properties[i]
        if i in seen:
            continue
        out.append(p)
        seen.update(range(i, i + max(p.array_dim, 1)))
    return out


def _reindex(st, props):
    st.properties = {}
    run = 0
    for p in props:
        dim = max(p.array_dim, 1)
        for j in range(dim):
            st.properties[run + j] = Property(run + j, p.name, dim, p.type)
        run += dim
    st.property_count = run


def union(paths):
    """Merge several .usmaps into one schema.

    No single dump is complete: some usmap dumps carry inheritance and static
    array dims but predate the current build; our own dumper matches the
    current build but has no supers and loses ArrayDim. Index lists of two
    builds must NEVER be mixed per index, so per struct the LAST source that
    has it is authoritative for field order/types; earlier sources contribute
    the super and, by name, a larger static-array dim. Indices are then
    re-assigned densely (dim-expanded) and property_count = sum of dims.
    """
    loaded = []
    for path in paths:
        try:
            loaded.append(load_usmap(path))
        except Exception as e:
            print(f"  [skip] {path}: {e}")
    if not loaded:
        raise ValueError("no readable usmap")
    base = loaded[0]
    for u in loaded[1:]:
        for name, st in u.structs.items():
            cur = base.structs.get(name)
            if cur is None:
                base.structs[name] = st
                continue
            old_dims = {p.name: p.array_dim for p in _own_props(cur)}
            props = []
            for p in _own_props(st):
                d = max(p.array_dim, old_dims.get(p.name, 1))
                props.append(Property(p.index, p.name, d, p.type))
            if not st.super and cur.super:
                st.super = cur.super
            _reindex(st, props)
            base.structs[name] = st
        for name, en in u.enums.items():
            if name not in base.enums or len(en) > len(base.enums[name]):
                base.enums[name] = en
    return base
