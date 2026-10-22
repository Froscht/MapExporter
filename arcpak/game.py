"""Shared game-data context and property reader used by the map tools.

The context (mounted containers, name tables, property schema) is built on FIRST USE, not on
import: importing this module costs nothing, so `arcpak` stays a plain library. Access it through
containers() / global_names() / script_map() / schema(); the old module attributes C, GN, smap,
sch still resolve lazily through module __getattr__ and are equivalent.
    C, GN       mounted IoStore containers, global name table
    smap        script-object map (class names for script imports)
    sch         property schema (usmap)
    read_unversioned / read_native_tail   unversioned property block + native trailer
    read_component, Templates             component fields incl. archetype inheritance

Unversioned properties store only values that differ from the archetype; missing fields are
resolved through the template chain (Templates.fields).
"""
from __future__ import annotations

import os
import re
import struct

from . import iostore, usmap, zen
from .globaldata import PAKS_DEFAULT, load_global_names, load_script_map
from .mesh import Walk
from .usmap import Property

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------- schema ------------------------------------------------------------
# Source: $ARC_USMAP, otherwise usmap/SDK_Mappings.usmap. Use a single dump: mixing usmap files
# changes per-class property counts and shifts every schema index after the first difference.
# Extra files only on purpose via USMAP_EXTRA="a.usmap:b.usmap".
USMAP = os.environ.get("ARC_USMAP") or os.path.join(HERE, "usmap", "SDK_Mappings.usmap")
SCHEMAS = [USMAP] + [p for p in os.environ.get("USMAP_EXTRA", "").split(os.pathsep) if p]

FREAL = 8                     # large world coordinates: FReal = double

SCALAR = {"BoolProperty": 1, "IntProperty": 4, "FloatProperty": 4, "UInt32Property": 4,
          "Int64Property": 8, "UInt64Property": 8, "Int16Property": 2, "UInt16Property": 2,
          "Int8Property": 1, "ByteProperty": 1, "DoubleProperty": 8,
          "ObjectProperty": 4, "SoftObjectProperty": 4, "WeakObjectProperty": 4,
          "ClassProperty": 4, "AssetObjectProperty": 4, "SoftClassProperty": 4,
          "InterfaceProperty": 8, "NameProperty": 8, "StrProperty": "fstring",
          "Utf8StrProperty": "fstring", "AnsiStrProperty": "fstring",
          "TextProperty": "text", "FieldPathProperty": 8,
          "DelegateProperty": 12, "MulticastDelegateProperty": "multicast",
          "MulticastInlineDelegateProperty": "multicast",
          "MulticastSparseDelegateProperty": "multicast",
          "SetProperty": "set", "ArrayProperty": "array", "MapProperty": "map",
          "OptionalProperty": "optional"}

# Native (binary) structs with a fixed size. "Transform" is not native (only Transform3f is).
KNOWN_STRUCT = {
    "Transform3f": 40,
    "Vector": 3 * FREAL, "Vector2D": 2 * FREAL, "Vector4": 4 * FREAL,
    "Rotator": 3 * FREAL, "Quat": 4 * FREAL,
    "LinearColor": 16, "Color": 4,
    "IntPoint": 8, "Int32Point": 8, "IntVector2": 8, "IntVector": 12, "IntVector4": 16,
    "UintVector2": 8, "UintVector": 12, "UintVector4": 16, "Int64Point": 16,
    "Guid": 16, "DateTime": 8, "Timespan": 8, "FrameNumber": 4,
    "Vector2f": 8, "Vector3f": 12, "Vector4f": 16, "Vector3d": 24, "Vector4d": 32,
    "Rotator3f": 12, "Quat4f": 16, "Plane": 4 * FREAL, "Plane4f": 16, "Sphere": 4 * FREAL,
    "Box": 6 * FREAL + 1, "Box2D": 4 * FREAL + 1, "Box2f": 17, "Box3f": 25,
    "Matrix": 16 * FREAL, "Matrix44f": 64, "TwoVectors": 6 * FREAL,
}

# Scalars whose value read_value returns (everything else returns None / the struct name)
SCALAR_CODE = {"BoolProperty": "<B", "IntProperty": "<i", "FloatProperty": "<f", "UInt32Property": "<I",
               "Int64Property": "<q", "UInt64Property": "<Q", "Int16Property": "<h",
               "UInt16Property": "<H", "Int8Property": "<b", "ByteProperty": "<B",
               "DoubleProperty": "<d", "ObjectProperty": "<i", "ClassProperty": "<i",
               "WeakObjectProperty": "<i", "NameProperty": "<II"}
REAL_STRUCT = {"Vector": 3, "Rotator": 3, "Quat": 4, "Vector2D": 2, "Vector4": 4}


class Unsupported(Exception):
    pass


class OutOfData(Exception):
    pass


def read_fstring(d, p):
    n, = struct.unpack_from("<i", d, p)
    if n == 0:
        return p + 4, ""
    if n > 0:
        return p + 4 + n, d[p + 4:p + 4 + n].decode("utf-8", "replace").rstrip("\0")
    m = -n
    return p + 4 + m * 2, d[p + 4:p + 4 + m * 2].decode("utf-16-le", "replace").rstrip("\0")


def _skip_softpath(d, p):                        # cooked: FTopLevelAssetPath + FString
    return read_fstring(d, p + 16)[0]


def _skip_tagcontainer(d, p):                    # i32 count + FName * count
    n, = struct.unpack_from("<i", d, p)
    return p + 4 + 8 * n


def _skip_fspline(d, p):                         # FSpline: 1 byte previousImpl, 0 when cooked
    prev, = struct.unpack_from("<b", d, p)
    if prev:
        raise Unsupported(f"FSpline previousImpl={prev}")
    return p + 1


def _perplatform(width):
    """TPerPlatformProperty: bool32 bCooked | Default. Cooked data always has bCooked=1."""
    def rd(d, p):
        ck = struct.unpack_from("<I", d, p)[0]
        if ck != 1:
            raise Unsupported(f"PerPlatform bCooked={ck}")
        return p + 4 + width
    return rd


def _skip_weighted_sampler(d, p):
    """FWeightedRandomSampler (natively serialized): TArray<float> Prob, TArray<int32> Alias,
    float TotalWeight."""
    n, = struct.unpack_from("<i", d, p); p += 4 + 4 * n
    m, = struct.unpack_from("<i", d, p); p += 4 + 4 * m
    return p + 4


KNOWN_VAR_STRUCT = {"SoftObjectPath": _skip_softpath, "SoftClassPath": _skip_softpath,
                    "GameplayTagContainer": _skip_tagcontainer, "Spline": _skip_fspline,
                    "PerPlatformInt": _perplatform(4), "PerPlatformFloat": _perplatform(4),
                    "PerPlatformBool": _perplatform(4), "PerPlatformFrameRate": _perplatform(8),
                    "SkeletalMeshSamplingLODBuiltData": _skip_weighted_sampler}

# ---------- context (lazy) -------------------------------------------------------
# Building the context costs about a second (mounting 200+ containers, schema union). Keep it out
# of import time so `import arcpak.placements` is free and the tools can build it once for several
# steps. _CACHE holds the singletons; reset_context() drops them (after a game patch).
_CACHE: dict = {}


def script_map() -> dict:
    """Raw FScriptObjectEntry.GlobalIndex -> (bare name, OuterIndex); class names for imports."""
    if "smap" not in _CACHE:
        _CACHE["smap"] = load_script_map(cache=os.path.join(HERE, "cache", "scriptobjects.blob"))
    return _CACHE["smap"]


def schema():
    """usmap property schema (union over SCHEMAS)."""
    if "sch" not in _CACHE:
        _CACHE["sch"] = usmap.union(SCHEMAS)
    return _CACHE["sch"]


def containers() -> list:
    """Mounted IoStore containers (split copies first)."""
    if "C" not in _CACHE:
        _CACHE["C"] = iostore.mount_directory(PAKS_DEFAULT)
    return _CACHE["C"]


def global_names() -> list:
    """Global FName table: index -> name."""
    if "GN" not in _CACHE:
        _CACHE["GN"] = load_global_names(cache=os.path.join(HERE, "cache", "globalnames.bin"))
    return _CACHE["GN"]


def templates() -> "Templates":
    """Shared Templates instance: the package index and the archetype-field caches survive between
    steps, so a following step (glb after placements) does not re-read the same packages."""
    if "T" not in _CACHE:
        _CACHE["T"] = Templates()
    return _CACHE["T"]


class Context:
    """The game-data context as one object, so steps can take it as an argument instead of
    reaching into module globals. Every attribute builds its part on first access."""

    @property
    def smap(self) -> dict:
        return script_map()

    @property
    def sch(self):
        return schema()

    @property
    def C(self) -> list:
        return containers()

    @property
    def GN(self) -> list:
        return global_names()

    @property
    def templates(self) -> "Templates":
        return templates()


def context() -> Context:
    """Context handle; cheap to create, the data behind it is shared."""
    return Context()


class LazyTemplates:
    """game.templates() behind an attribute proxy: module-level `T = game.LazyTemplates()` touches
    no container until a step actually reads a field."""

    def __getattr__(self, name):
        return getattr(templates(), name)


def reset_context() -> None:
    """Forget the mounted context (containers stay open until garbage collected)."""
    _CACHE.clear()
    _chain_cache.clear()
    _index_maps.clear()


def __getattr__(name: str):
    """Module attributes C / GN / smap / sch resolve lazily (PEP 562)."""
    if name == "C":
        return containers()
    if name == "GN":
        return global_names()
    if name == "smap":
        return script_map()
    if name == "sch":
        return schema()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

_STEM_PREFIX = re.compile(r"^(?:\.\.?/)+")


def stem_of(path: str) -> str:
    """Grouping key for package + .ubulk pairs: path without extension, lower case
    (bulk files and their package can differ in letter case)."""
    return os.path.splitext(_STEM_PREFIX.sub("", path))[0].lower()


# ---------- schema lookup ---------------------------------------------------------
_chain_cache = {}


def chain_of(cls_name):
    """[derived .. base] struct list."""
    if cls_name in _chain_cache:
        return _chain_cache[cls_name]
    structs = schema().structs
    out, s, seen = [], structs.get(cls_name), 0
    while s is not None and seen < 14:
        out.append(s)
        s = structs.get(s.super) if s.super else None
        seen += 1
    _chain_cache[cls_name] = out
    return out


def chain_names(cls):
    try:
        return [s.name for s in chain_of(cls)]
    except Exception:
        return []


_index_maps = {}


def chain_lookup(cls_name: str, idx: int):
    """Schema index -> Property. Indices run along the flattened chain from the derived class
    to the base, each struct contributing its own property count."""
    m = _index_maps.get(cls_name)
    if m is None:
        m, run = {}, 0
        for st in chain_of(cls_name):
            for local, prop in sorted(st.properties.items()):
                m[run + local] = prop
            run += len(st.properties)
        _index_maps[cls_name] = m
    return m.get(idx)


# ---------- property reader -------------------------------------------------------
def read_value(d, p, end, prop, depth=0):
    t = prop.type.type
    if t in SCALAR:
        w = SCALAR[t]
        if w == "fstring":
            return read_fstring(d, p)
        if w == "multicast":
            cnt, = struct.unpack_from("<i", d, p); p += 4
            p += cnt * (4 + 8)                  # FPackageIndex + FName
            return p, f"[{cnt} delegates]"
        if w == "array":
            cnt, = struct.unpack_from("<i", d, p); p += 4
            inner = prop.type.inner
            if inner is None:
                raise Unsupported("array without inner")
            vals = []
            for _ in range(cnt):
                p, v = read_value(d, p, end, Property(0, prop.name + "[]", 1, inner), depth + 1)
                vals.append(v)
            return p, vals
        if w == "map":
            p += 8                              # array/value length
            num_keys, = struct.unpack_from("<i", d, p); p += 4
            p += 5 + 5                          # FBitArray headers
            k, v = prop.type.inner, prop.type.value
            if k is None or v is None:
                raise Unsupported("map without key/value")
            for _ in range(num_keys):
                p, _ = read_value(d, p, end, Property(0, "k", 1, k), depth + 1)
            for _ in range(num_keys):
                p, _ = read_value(d, p, end, Property(0, "v", 1, v), depth + 1)
            p += 4                              # empty sparse-array state
            return p, f"map<{num_keys}>"
        if w == "set":
            cnt, = struct.unpack_from("<i", d, p); p += 4
            p += 5 + 5
            for _ in range(cnt):
                p, _ = read_value(d, p, end, Property(0, "e", 1, prop.type.inner), depth + 1)
            p += 4
            return p, f"set<{cnt}>"
        if w == "optional":
            flag, = struct.unpack_from("<I", d, p); p += 4      # bIsSet is serialized as a 32-bit bool
            if flag and prop.type.inner:
                return read_value(d, p, end, Property(0, prop.name, 1, prop.type.inner), depth + 1)
            return p, None
        if p + w > end:
            raise OutOfData(f"{t} needs {w} at {p - end}")
        code = SCALAR_CODE.get(t)
        return p + w, (struct.unpack_from(code, d, p)[0] if code else None)
    if t == "EnumProperty":
        inner = prop.type.inner or prop.type.base   # stores base type + enum name
        w = SCALAR.get(inner.type, 1) if inner is not None else 1
        w = w if isinstance(w, int) else 1
        return p + w, f"enum:{prop.type.enum}:{w}"
    if t == "StructProperty":
        sn = prop.type.struct
        if sn in KNOWN_STRUCT:
            sz = KNOWN_STRUCT[sn]
            if p + sz > end:
                raise OutOfData(f"struct {sn} needs {sz}")
            if sn in REAL_STRUCT:
                return p + sz, struct.unpack_from("<%dd" % REAL_STRUCT[sn], d, p)
            if sn == "LinearColor":
                return p + sz, struct.unpack_from("<4f", d, p)
            return p + sz, sn
        if sn in ("SoftObjectPath", "SoftClassPath"):
            # cooked: FTopLevelAssetPath (FName package, FName asset) + FString sub path;
            # returned as name indices, resolve with pkg.names (see soft_path)
            pkg_name, asset_name = struct.unpack_from("<I4xI", d, p)
            q, sub = read_fstring(d, p + 16)
            return q, ("soft", pkg_name, asset_name, sub)
        if sn in KNOWN_VAR_STRUCT:
            return KNOWN_VAR_STRUCT[sn](d, p), sn
        if sn is None or sn not in schema().structs:
            raise Unsupported(f"struct {sn!r} not in schema")
        return read_unversioned(d, p, end, sn, depth + 1)
    raise Unsupported(f"type {t}")


def read_unversioned(d, p, end, cls_name, depth=0):
    """Unversioned property block -> (end position, [(name, value | "ZERO")])."""
    head = zen.UnversionedHeader(d, p)
    p = head.pos
    props = []
    if not head.has_values:
        return p, props
    for idx, is_nonzero in zen.FIterator(head):
        prop = chain_lookup(cls_name, idx)
        if prop is None:
            raise Unsupported(f"{cls_name}: unknown schema index {idx}")
        if not is_nonzero:
            props.append((prop.name, "ZERO"))   # zero values consume no bytes
            continue
        p, v = read_value(d, p, end, prop, depth)
        props.append((prop.name, v))
    return p, props


def read_native_tail(d, p, end, cls_name, props, is_cdo, bounds_out=None):
    """Native data that UObject / component Serialize() writes after the property block.
    bounds_out: optional dict, receives the cooked FBoxSphereBounds (7 reals)."""
    if not is_cdo:                                   # UObject: bHasGuid (+FGuid)
        if p + 4 > end:
            return p
        has_guid, = struct.unpack_from("<I", d, p); p += 4
        if has_guid:
            p += 16
    chain = [st.name for st in chain_of(cls_name)]
    if "MaterialInterface" in chain and p + 4 <= end:  # bSavedCachedExpressionData
        flag, = struct.unpack_from("<I", d, p); p += 4
        if flag:
            p, _ = read_unversioned(d, p, end, "MaterialCachedExpressionData", 1)
    if "MaterialInstance" in chain and p + 4 <= end:   # bSavedCachedData
        flag, = struct.unpack_from("<I", d, p); p += 4
        if flag:
            p, _ = read_unversioned(d, p, end, "MaterialInstanceCachedData", 1)
    if "ActorComponent" in chain and p < end:        # UCSModifiedProperties
        n, = struct.unpack_from("<i", d, p); p += 4
        p += n * (4 + 8 + 16)                        # FPackageIndex + FName + FGuid
    # USceneComponent::Serialize runs before UStaticMeshComponent (super first)
    if "SceneComponent" in chain and p < end:
        names = {k for k, _ in props}
        if names & {"bComputeBoundsOnceForGame", "bComputedBoundsOnceForGame"}:
            cooked, = struct.unpack_from("<I", d, p); p += 4
            if cooked:
                if bounds_out is not None:
                    bounds_out["bounds"] = struct.unpack_from("<7d", d, p)
                p += 7 * FREAL                       # FBoxSphereBounds
    if "StaticMeshComponent" in chain and p + 4 <= end:
        n, = struct.unpack_from("<i", d, p)
        if n != 0:                                   # FStaticMeshComponentLODInfo: native, not read
            return p
        p += 4
        if p + 4 <= end:                             # bSerializeAsCookedData (+FPackageIndex)
            cooked, = struct.unpack_from("<I", d, p); p += 4
            if cooked:
                p += 4
    return p


def native_start(raw, pkg, e):
    """(export start, start of the native StaticMesh body)."""
    a = pkg.header_size + e.offset
    q, props = read_unversioned(raw, a, a + e.size, "StaticMesh")
    q = read_native_tail(raw, q, a + e.size, "StaticMesh", props, bool(e.object_flags & 0x10))
    return a, q


def soft_path(pkg, v):
    """("soft", package, asset, sub) from read_value -> "/Game/.../Asset" (or None)."""
    if not (isinstance(v, tuple) and len(v) == 4 and v[0] == "soft"):
        return None
    names = pkg.names
    return names[v[1]] if v[1] < len(names) else None


# ---------- components ------------------------------------------------------------
def ref(pkg, v):
    """FPackageIndex (i32) -> ("export", i) | ("import", path, hash) | ("script", id) | None"""
    if v is None or v == 0:
        return None
    if v > 0:
        return ("export", v - 1)
    i = -v - 1
    if i >= len(pkg.imports):
        return ("bad", v)
    o = pkg.imports[i]
    r = pkg.resolve_package_import(o)
    if r:
        return ("import",) + r
    return ("script", o)


def ism_tail(raw, q, end, chain, out):
    """UInstancedStaticMeshComponent native data (UE 5.4+) + HISM cluster tree."""
    w = Walk(raw, q, end)
    cooked, skip = w.i32(), w.i32()
    if skip:
        out["instances"] = w.bulk_array()                      # FMatrix (16 doubles) per instance
        out["custom"] = w.bulk_array()
    if cooked:
        if w.i32():
            w.bulk_array(); w.bulk_array()
    if "HierarchicalInstancedStaticMeshComponent" in chain and w.p < end:
        out["cluster"] = w.bulk_array()                        # FClusterNode, 64 bytes each
    return w.p


def read_component(raw, pkg, e, cls, bounds_out=None, partial=False):
    """-> (block ends exactly at export end, props dict, end offset - export end).
    partial=True keeps the properties when only the native tail fails (needed for parent
    chains: the relative transform of e.g. a SplineComponent parent is still valid)."""
    a = pkg.header_size + e.offset
    end = a + e.size
    q, props = read_unversioned(raw, a, end, cls)
    tail_ok = True
    try:
        q = read_native_tail(raw, q, end, cls, props, bool(e.object_flags & 0x10), bounds_out)
    except Exception:
        if not partial:
            raise
        tail_ok = False
    if bounds_out and "bounds" in bounds_out:
        props.append(("bounds", bounds_out["bounds"]))
    props = dict(props)
    if tail_ok and "InstancedStaticMeshComponent" in chain_names(cls):
        try:
            q = ism_tail(raw, q, end, chain_names(cls), props)
        except Exception:
            if not partial:
                raise
            tail_ok = False
    return q == end, props, q - end


FIELDS = ("StaticMesh", "RelativeLocation", "RelativeRotation", "RelativeScale3D", "AttachParent",
          "AttachSocketName", "bHiddenInGame", "bVisible",
          "SplineParams", "ForwardAxis", "SplineUpDir",
          "SplineBoundaryMin", "SplineBoundaryMax", "bSmoothInterpRollScale")

# Fields inherited from the archetype when the export does not override them. Visibility flags
# matter: Blueprint helpers (light geometry, collision boxes, markers) are often hidden only in
# their template, so without inheriting them hidden components end up in the export.
INHERITED = ("StaticMesh", "RelativeLocation", "RelativeRotation", "RelativeScale3D",
             "bHiddenInGame", "bVisible", "OverrideMaterials")


class Templates:
    """Resolve the archetype chain: template index -> (package, public export hash) -> export."""

    def __init__(self, container_list=None):
        self.uidx = iostore.path_index(container_list or containers(), exts=(".uasset",))
        self.by_game = {}
        for k in self.uidx:
            if "/Content/" in k:
                head, rest = k.split("/Content/", 1)
                # import paths: /Game/... = project content, /Engine/... = engine content
                pref = "/engine/" if head.rstrip("/").lower().endswith("engine") else "/game/"
                self.by_game[pref + rest.rsplit(".", 1)[0].lower()] = k
        self.pk, self.cache = {}, {}

    def package(self, gpath):
        if gpath not in self.pk:
            k = self.by_game.get(gpath.lower())
            if k is None:
                self.pk[gpath] = None
            else:
                c, ci = self.uidx[k][0]
                raw = c.get_chunk(ci)
                pkg = zen.parse_uasset(raw, global_names=global_names())
                self.pk[gpath] = (raw, pkg, {e.public_hash: e for e in pkg.exports})
        return self.pk[gpath]

    def forget(self, pkg):
        """Drop cached fields of a finished cell (keys hold the pkg object alive)."""
        for k in [k for k in self.cache if k[0] is pkg]:
            del self.cache[k]

    def fields(self, raw, pkg, e, depth=0):
        """Fields of this export incl. the template chain; StaticMesh as ("import", path, hash).
        Keyed by the pkg object, not id(pkg): ids are reused after a cell is freed."""
        key = (pkg, e.index)
        if key in self.cache:
            return self.cache[key]
        cls = pkg.export_class(e, script_names=script_map())
        out = {}
        try:
            ok, pr, _ = read_component(raw, pkg, e, cls, {}, partial=True)
        except Exception:
            ok, pr = False, {}
        for f in FIELDS:
            v = pr.get(f)
            if v not in (None, "ZERO"):
                out[f] = ref(pkg, v) if f in ("StaticMesh", "AttachParent") else v
        om = pr.get("OverrideMaterials")
        if isinstance(om, list) and om:
            # material names (first slots only; read_value keeps the first three array values)
            names = []
            for v in om:
                rr = ref(pkg, v) if isinstance(v, int) else None
                names.append(rr[1].rsplit("/", 1)[-1] if rr and rr[0] == "import" else None)
            if any(names):
                out["OverrideMaterials"] = names
        out["_ok"] = ok
        out["_class"] = cls
        if "instances" in pr:
            out["instances"] = pr["instances"]
        if "bounds" in pr:
            out["bounds"] = pr["bounds"]
        t = e.template_idx
        base = None
        if depth < 8 and pkg.idx_type(t) == "package_import":
            r = pkg.resolve_package_import(t)
            tp = self.package(r[0]) if r else None
            te = tp[2].get(r[1]) if tp else None
            if te is not None:
                base = self.fields(tp[0], tp[1], te, depth + 1)
        elif depth < 8 and pkg.idx_type(t) == "export":
            base = self.fields(raw, pkg, pkg.exports[pkg.idx_id(t)], depth + 1)
        # "instances" is never inherited: its offsets are absolute in the template package
        for f in INHERITED if base else ():
            if f not in out and f in base:
                out[f] = base[f]
        self.cache[key] = out
        return out
