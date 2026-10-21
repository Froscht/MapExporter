"""Model catalogue (weapons, ARC enemies, gear, character outfits), mesh loading and GLB export.

Characters are assembled like the game does it:
    outfit  DA_OI_Outfit_<Name>  (CharacterVisualSkinOnlineItemDataAsset): Parts = visual parts
    part    DA_VP_*              (CustomizationVisualPartDataAsset): imports its mesh package
    toggles DA_OI_Outfit_<Name>_<Setting> (slot) -> default option from DefaultItemQuery
            ("Character.Option.On" ...) -> DA_OI_Outfit_<Name>_<Setting>_<Option>, whose
            CustomizationVisualPartSetVisibilityModifier exports switch parts on (bVisibility)
            or off (no bVisibility)
    body    Characters/Shared/Bodies/SK_Body, head from DA_OI_Face_<Face> -> Part_Head_* -> Head_*
Weapons come from DA_WeaponVisuals_<Weapon> (WeaponVisualsDataAsset): a base mesh plus WeaponParts,
each attached to a bone or socket of the base (AttachmentSocket) with a local offset/rotation/scale.
Meshes are taken in their reference pose (LOD0); all character parts share one skeleton space.
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field

from . import game, gltf, iostore, mesh, zen
from . import uetransform as ut

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(HERE, "out", "models")
CONTENT = "../../../PioneerGame/Content/"
BODY = "/Game/Pioneer/Characters/Shared/Bodies/SK_Body"
CATEGORIES = ("weapons", "bots", "gear", "characters")

_index = None
_lower = None                           # lower-case path -> path (asset references ignore case)
_pkg_cache = {}
_index_lock = threading.Lock()          # two GUI tabs may ask for the index at the same time


@dataclass
class MeshData:
    name: str
    pos: list
    idx: list
    sections: list                      # (material, first index, ntri, first vertex, last vertex)
    nrm: list | None = None
    uv: list | None = None
    materials: list = field(default_factory=list)
    material_paths: list = field(default_factory=list)  # /Game/... per slot (None if unknown)
    textures: dict = field(default_factory=dict)        # slot -> Pillow image (apply_textures)
    colors: dict = field(default_factory=dict)          # slot -> (r, g, b) for slots without texture
    bone_names: list = field(default_factory=list)      # skeleton (SkeletalMesh only)
    parents: list = field(default_factory=list)
    pose: list = field(default_factory=list)            # local (quat, translation, scale) per bone
    joints: list | None = None                          # 4 bone indices per vertex
    weights: list | None = None                         # 4 weights per vertex
    rigid_bone: str | None = None                       # static part attached to this bone

    @property
    def triangles(self):
        return sum(s[2] for s in self.sections)


@dataclass
class Item:
    category: str
    name: str
    key: str                            # mesh package, weapon visuals asset or outfit asset (/Game/...)
    folder: str = ""


# ---------- package access ----------------------------------------------------------------
def index():
    global _index, _lower
    if _index is None:
        with _index_lock:
            if _index is None:
                _index = iostore.path_index(game.C, exts=(".uasset", ".ubulk"))
                _lower = {k.lower(): k for k in _index}
    return _index


def lookup(key: str):
    """Container entries of a package file, case-insensitive like Unreal asset paths."""
    idx = index()
    hits = idx.get(key)
    if hits is None:
        real = _lower.get(key.lower())
        hits = idx.get(real) if real else None
    return hits


def exists(gpath: str) -> bool:
    return lookup(to_key(gpath)) is not None


def to_key(gpath: str, ext=".uasset") -> str:
    """/Game/Pioneer/X -> container path of the package file."""
    return CONTENT + gpath[len("/Game/"):].split(".")[0] + ext


def to_gpath(key: str) -> str:
    return "/Game/" + key[len(CONTENT):].rsplit(".", 1)[0]


def package(gpath: str):
    """(raw, pkg) of a /Game/ package, small cache."""
    if gpath in _pkg_cache:
        return _pkg_cache[gpath]
    hits = lookup(to_key(gpath))
    if not hits:
        raise KeyError(gpath)
    c, ci = hits[0]
    raw = c.get_chunk(ci)
    pkg = zen.parse_uasset(raw, global_names=game.GN)
    if len(_pkg_cache) > 256:
        _pkg_cache.clear()
    _pkg_cache[gpath] = (raw, pkg)
    return raw, pkg


def exports(gpath: str):
    """[(export, class name, props dict | None)] of a package."""
    raw, pkg = package(gpath)
    out = []
    for e in pkg.exports:
        cls = pkg.export_class(e, script_names=game.smap)
        a = pkg.header_size + e.offset
        try:
            _q, props = game.read_unversioned(raw, a, a + e.size, cls)
            props = dict(props)
        except Exception:
            props = None
        out.append((e, cls, props))
    return out


def mesh_class(gpath: str):
    try:
        _raw, pkg = package(gpath)
    except KeyError:
        return None
    for e in pkg.exports:
        cls = pkg.export_class(e, script_names=game.smap)
        if cls in ("SkeletalMesh", "StaticMesh"):
            return cls
    return None


# ---------- mesh loading ------------------------------------------------------------------
def _mat_name(pkg, v, fallback):
    r = game.ref(pkg, v) if isinstance(v, int) and v else None
    if r and r[0] == "import" and isinstance(r[1], str):
        return r[1].rsplit("/", 1)[-1]
    return fallback


def _mat_path(pkg, v):
    r = game.ref(pkg, v) if isinstance(v, int) and v else None
    return r[1] if r and r[0] == "import" and isinstance(r[1], str) else None


def load_mesh(gpath: str) -> MeshData:
    """LOD0 of a SkeletalMesh or StaticMesh package."""
    raw, pkg = package(gpath)
    for e in pkg.exports:
        cls = pkg.export_class(e, script_names=game.smap)
        if cls == "SkeletalMesh":
            return _load_skeletal(gpath, raw, pkg, e)
        if cls == "StaticMesh":
            return _load_static(gpath, raw, pkg, e)
    raise ValueError(f"{gpath}: no mesh export")


def _load_skeletal(gpath, raw, pkg, e):
    a = pkg.header_size + e.offset
    q, props = game.read_unversioned(raw, a, a + e.size, "SkeletalMesh")
    q = game.read_native_tail(raw, q, a + e.size, "SkeletalMesh", props, bool(e.object_flags & 0x10))
    st = mesh.walk_skeletal(raw, q, a + e.size)
    lod = st["lods"][0]
    b = lod["buffers"]
    if b is None:
        raise ValueError(f"{gpath}: LOD0 cooked out")
    names = pkg.names
    mats = [_mat_name(pkg, mi, names[ni] if ni < len(names) else f"mat{i}")
            for i, (mi, ni) in enumerate(st["materials"])]
    md = MeshData(gpath.rsplit("/", 1)[-1], mesh.positions(raw, b), mesh.indices(raw, b["index"]),
                  lod["sections"], mesh.normals(raw, b), mesh.uvs(raw, b), mats,
                  [_mat_path(pkg, mi) for mi, _ni in st["materials"]])
    md.bone_names = [names[ni] if ni < len(names) else f"bone{i}" for i, (ni, _par) in enumerate(st["bones"])]
    md.bones = {n: i for i, n in enumerate(md.bone_names)}
    md.parents = [par for _ni, par in st["bones"]]
    md.pose = st["pose"]
    md.joints, md.weights = mesh.skin_weights(raw, b, lod["sections"], lod["bonemaps"])
    sk = dict(props).get("Skeleton")
    md.skeleton = game.ref(pkg, sk) if isinstance(sk, int) and sk else None
    return md


def _load_static(gpath, raw, pkg, e):
    a, q = game.native_start(raw, pkg, e)
    st = mesh.walk_native(raw, q, a + e.size)
    lod = st["lods"][0]
    if lod["kind"] == "inline":
        d, b = raw, lod["buffers"]
    elif lod["kind"] == "bulk":
        o, _dup, size, flags, _ci = pkg.bulk_data_map()[lod["bulk"]]
        hits = lookup(to_key(gpath, ".ubulk"))
        if not hits or not flags & mesh.SEP:
            raise ValueError(f"{gpath}: bulk data not found")
        c, ci = hits[0]
        d = c.get_chunk(ci)[o:o + size]
        w = mesh.Walk(d, 0, len(d))
        b = mesh.read_buffers(w, len(lod["sections"]))
    else:
        raise ValueError(f"{gpath}: LOD0 empty")
    _q, pr = game.read_unversioned(raw, a, a + e.size, "StaticMesh")
    mats, paths = [], []
    for i, sm in enumerate(dict(pr).get("StaticMaterials") or []):
        mats.append(_mat_name(pkg, dict(sm).get("MaterialInterface"), f"mat{i}"))
        paths.append(_mat_path(pkg, dict(sm).get("MaterialInterface")))
    return MeshData(gpath.rsplit("/", 1)[-1], mesh.positions(d, b), mesh.indices(d, b["index"]),
                    lod["sections"], mesh.normals(d, b), mesh.uvs(d, b), mats, paths)


# ---------- transforms / sockets -----------------------------------------------------------------
def transformed(md: MeshData, T: ut.Transform, name=None) -> MeshData:
    """Copy of a mesh with positions and normals transformed (UE coordinates)."""
    rot = ut.Transform(T.r, (0.0, 0.0, 0.0), (1.0, 1.0, 1.0))
    nrm = [tuple(rot.apply(n)) for n in md.nrm] if md.nrm is not None else None
    out = MeshData(name or md.name, [T.apply(p) for p in md.pos], md.idx, md.sections, nrm, md.uv, md.materials,
                   md.material_paths, md.textures, md.colors)
    out.rigid_bone = md.rigid_bone
    return out


def bone_world(md: MeshData, i: int) -> ut.Transform:
    """Reference-pose transform of bone i in mesh space."""
    T = ut.Transform(*md.pose[i])
    p = md.parents[i]
    while p >= 0:
        T = T * ut.Transform(*md.pose[p])          # child * parent = child in parent space
        p = md.parents[p]
    return T


def _vec(v):
    return None if v in (None, "ZERO") else tuple(v)


def _sockets(gpath):
    """{socket name: (bone name | None, Transform)} from SkeletalMeshSocket / StaticMeshSocket exports."""
    out = {}
    try:
        _raw, pkg = package(gpath)
    except KeyError:
        return out
    names = pkg.names
    for e, cls, props in exports(gpath):
        if cls in ("SkeletalMeshSocket", "StaticMeshSocket") and props:
            name, bone = props.get("SocketName"), props.get("BoneName")
            rel = ut.Transform.from_relative(_vec(props.get("RelativeLocation")), _vec(props.get("RelativeRotation")),
                                             _vec(props.get("RelativeScale")))
            if isinstance(name, int) and name < len(names):
                out[names[name]] = (names[bone] if isinstance(bone, int) and bone < len(names) else None, rel)
    return out


def socket_transform(base: MeshData, base_gpath: str, extra_gpaths, socket: str):
    """(transform in mesh space, bone it moves with) of a bone or socket of the base mesh;
    (None, None) if unknown."""
    if socket in (None, "None", ""):
        return ut.Transform(), None
    key = socket.lower()                        # FName comparison ignores case
    bones = {k.lower(): v for k, v in getattr(base, "bones", {}).items()}
    if key in bones:
        return bone_world(base, bones[key]), base.bone_names[bones[key]]
    for g in [base_gpath, *extra_gpaths]:
        sk = {k.lower(): v for k, v in _sockets(g).items()}
        if key in sk:
            bone, rel = sk[key]
            if bone and bone.lower() in bones:
                i = bones[bone.lower()]
                return rel * bone_world(base, i), base.bone_names[i]
            return rel, None
    return None, None


# ---------- catalogue -----------------------------------------------------------------------
_SKIP = re.compile(r"_(Skeleton|Physics|PhysicsAsset|CtrlRig|NullMesh)$|Proxy", re.I)


def _pretty(stem: str) -> str:
    s = re.sub(r"^(SK|SKM|SM)_", "", stem)
    return s.replace("_", " ")


def catalogue() -> dict[str, list[Item]]:
    """Items per category, from package names only (fast; meshes are validated on load)."""
    cat = {c: [] for c in CATEGORIES}
    keys = [k for k in index() if k.endswith(".uasset")]
    loose_weapons = {}                          # weapon folder -> skeletal mesh (no visuals asset)
    for k in keys:
        stem = k.rsplit("/", 1)[-1][:-len(".uasset")]
        if not stem.startswith(("SK_", "SKM_")) or _SKIP.search(stem):
            continue
        g = to_gpath(k)
        if "/Items/Firearms/" in k:
            loose_weapons.setdefault(k.split("/Firearms/")[1].split("/")[0], g)
            continue                                # weapons come from their visuals asset below
        elif "/Pioneer/Enemies/" in k:
            cat["bots"].append(Item("bots", _pretty(stem), g, k.split("/Enemies/")[1].split("/")[0]))
        elif "/Characters/Backpacks/" in k or "/Characters/RaiderTool/" in k:
            cat["gear"].append(Item("gear", _pretty(stem), g))
        elif "/Pioneer/Items/Armor/" in k:
            # deployable armor bodies (skeletal, with Deploy/Damaged clips)
            cat["gear"].append(Item("gear", _pretty(stem).replace("01 Default ", ""), g))
    for k in keys:
        if "/Items/Firearms/" in k:
            folder = k.split("/Firearms/")[1].split("/")[0]
            if k.rsplit("/", 1)[-1] == f"DA_WeaponVisuals_{folder}.uasset":
                cat["weapons"].append(Item("weapons", folder.replace("_", " "), to_gpath(k), folder))
            continue
        if "/Skins/Outfit/" not in k:
            continue
        stem = k.rsplit("/", 1)[-1][:-len(".uasset")]
        folder = k.split("/Skins/Outfit/")[1].split("/")[0]
        if stem == f"DA_OI_Outfit_{folder}":
            cat["characters"].append(Item("characters", folder, to_gpath(k), folder))
    have = {it.folder for it in cat["weapons"]}
    for folder, g in loose_weapons.items():
        if folder not in have:
            cat["weapons"].append(Item("weapons", folder.replace("_", " "), g, folder))
    for v in cat.values():
        v.sort(key=lambda it: it.name.lower())
    return cat


def faces() -> list[str]:
    out = set()
    for k in index():
        if "/Skins/Face/" in k and k.rsplit("/", 1)[-1].startswith("DA_OI_Face_") and "Persistence" not in k:
            out.add(k.split("/Skins/Face/")[1].split("/")[0])
    return sorted(out)


# ---------- outfits -------------------------------------------------------------------------
def _soft_list(pkg, v):
    return [p for p in (game.soft_path(pkg, x) for x in (v or [])) if p]


def part_mesh(vp_gpath: str):
    """Mesh package imported by a visual part (DA_VP_* / Part_Head_*), or None (toggle-only parts)."""
    try:
        _raw, pkg = package(vp_gpath)
    except KeyError:
        return None
    meshes = [imp for imp in pkg.imported_package_names() or [] if mesh_class(imp)]
    # heads also import a wrap-deformer mesh (*_Wraps); prefer the actual mesh
    meshes.sort(key=lambda m: m.endswith("_Wraps"))
    return meshes[0] if meshes else None


def _default_option(slot_gpath: str):
    """Option tag name ("On", "Off", "Basic", ...) the slot uses by default."""
    raw, pkg = package(slot_gpath)
    for e, cls, props in exports(slot_gpath):
        if cls == "CharacterVisualSlotOnlineItemDataAsset" and props and "DefaultItemQuery" in props:
            tags = [pkg.names[t[0][1]] for t in dict(props["DefaultItemQuery"]).get("TagDictionary", [])]
            opts = [t for t in tags if t.startswith("Character.Option.")]
            if opts:
                return opts[-1].rsplit(".", 1)[-1]
    return None


def outfit_parts(outfit_gpath: str) -> list[str]:
    """Visual parts of an outfit that are visible with the default toggle settings."""
    _raw, pkg = package(outfit_gpath)
    parts = []
    for e, cls, props in exports(outfit_gpath):
        if cls == "CharacterVisualSkinOnlineItemDataAsset" and props:
            parts = _soft_list(pkg, props.get("Parts"))
    visible = {p: True for p in parts}
    base = outfit_gpath.rsplit("/", 1)[0] + "/"
    stem = outfit_gpath.rsplit("/", 1)[1]
    folder = to_key(outfit_gpath).rsplit("/", 1)[0] + "/"
    slots = sorted({k.rsplit("/", 1)[1][:-len(".uasset")] for k in index()
                    if k.startswith(folder) and k.endswith(".uasset")
                    and re.fullmatch(re.escape(stem) + r"_[A-Za-z]+\d*", k.rsplit("/", 1)[1][:-len(".uasset")])})
    for slot in slots:
        try:
            opt = _default_option(base + slot)
        except Exception:
            continue
        if not opt:
            continue
        item = base + f"{slot}_{opt}"
        if not exists(item):
            continue
        _r, ipkg = package(item)
        for e, cls, props in exports(item):
            if cls == "CustomizationVisualPartSetVisibilityModifier" and props is not None:
                part = game.soft_path(ipkg, props.get("Part"))
                if part:
                    visible[part] = bool(props.get("bVisibility"))
    return [p for p in parts if visible.get(p, True)]


def outfit_materials(outfit_gpath: str) -> dict[str, str]:
    """{visual part (lower case): material} of the outfit's default color variant."""
    base = outfit_gpath.rsplit("/", 1)[0] + "/"
    slot = outfit_gpath + "_Color"
    if not exists(slot):
        return {}
    try:
        opt = _default_option(slot)
    except Exception:
        return {}
    item = f"{slot}_{opt}" if opt else None
    if not item or not exists(item):
        return {}
    _r, pkg = package(item)
    out = {}
    for e, cls, props in exports(item):
        if cls == "CustomizationVisualPartSetMaterialModifier" and props:
            part, mat = game.soft_path(pkg, props.get("Part")), game.soft_path(pkg, props.get("Material"))
            if part and mat:
                out[part.lower()] = mat
    return out


def face_mesh(face: str):
    g = f"/Game/Pioneer/Items/Characters/Skins/Face/{face}/DA_OI_Face_{face}"
    try:
        _raw, pkg = package(g)
    except KeyError:
        return None
    for e, cls, props in exports(g):
        if props and "Parts" in props:
            for vp in _soft_list(pkg, props["Parts"]):
                m = part_mesh(vp)
                if m:
                    return m
    return None


def _import_path(pkg, v):
    r = game.ref(pkg, v) if isinstance(v, int) and v else None
    return r[1] if r and r[0] == "import" else None


def weapon_parts(visuals_gpath: str):
    """(base mesh, base static mesh, [(part mesh, socket, local Transform)]) of a WeaponVisualsDataAsset."""
    _raw, pkg = package(visuals_gpath)
    for e, cls, props in exports(visuals_gpath):
        if cls != "WeaponVisualsDataAsset" or not props:
            continue
        base_sm = _import_path(pkg, props.get("BasePartStaticMesh"))
        base = _import_path(pkg, props.get("BasePartSkeletalMesh")) or base_sm
        parts = []
        for part in props.get("WeaponParts") or []:
            d = dict(part)
            m = _import_path(pkg, d.get("SkeletalMesh")) if d.get("bUseSkeletalMesh") == 1 else None
            m = m or _import_path(pkg, d.get("Mesh"))
            if not m:
                continue
            sock = d.get("AttachmentSocket")
            sock = pkg.names[sock] if isinstance(sock, int) and sock < len(pkg.names) else None
            local = ut.Transform.from_relative(_vec(d.get("LocalOffset")), _vec(d.get("LocalRotation")),
                                               _vec(d.get("LocalScale")))
            parts.append((m, sock, local))
        return base, base_sm, parts
    raise ValueError(f"{visuals_gpath}: no WeaponVisualsDataAsset")


def item_meshes(item: Item, body=True, face: str | None = None) -> list[str]:
    """Mesh packages that make up an item."""
    if item.category == "weapons" and "/DA_WeaponVisuals_" in item.key:
        base, _sm, parts = weapon_parts(item.key)
        return [base] + [m for m, _s, _l in parts]
    if item.category != "characters":
        return [item.key]
    out = [BODY] if body else []
    if face:
        f = face_mesh(face)
        if f:
            out.append(f)
    for vp in outfit_parts(item.key):
        m = part_mesh(vp)
        if m and m not in out:
            out.append(m)
    return out


def build(item: Item, body=True, face: str | None = None):
    """(meshes in their final position, warnings) for an item."""
    warnings = []
    if item.category == "weapons" and "/DA_WeaponVisuals_" in item.key:
        base_g, base_sm, parts = weapon_parts(item.key)
        base = load_mesh(base_g)
        skel = getattr(base, "skeleton", None)
        extra = [x for x in (base_sm, skel[1] if skel and skel[0] == "import" else None) if x]
        out = [base]
        for g, sock, local in parts:
            try:
                md = load_mesh(g)
            except Exception as e:
                warnings.append(f"{g.rsplit('/', 1)[-1]}: {e}")
                continue
            # unknown socket: the engine attaches to the component root, so does this
            T, bone = socket_transform(base, base_g, extra, sock)
            part = transformed(md, local * (T or ut.Transform()))
            part.rigid_bone = bone or (base.bone_names[0] if base.bone_names else None)
            out.append(part)
        return out, warnings
    out = []
    overrides = {}                              # mesh package -> material of the color variant
    if item.category == "characters":
        mats = outfit_materials(item.key)
        for vp in outfit_parts(item.key):
            m = part_mesh(vp)
            if m and vp.lower() in mats:
                overrides[m] = mats[vp.lower()]
    for g in item_meshes(item, body=body, face=face):
        try:
            md = load_mesh(g)
        except Exception as e:
            warnings.append(f"{g.rsplit('/', 1)[-1]}: {e}")
            continue
        if g in overrides:
            from . import texture
            for slot, mp in enumerate(md.material_paths):
                # only the layered outfit material is swapped (not glass, decals, ...)
                if mp and "ColorMask" in texture.material_params(mp)[0]:
                    md.material_paths[slot] = overrides[g]
        out.append(md)
    return out, warnings


def apply_textures(meshes: list[MeshData], max_size: int = 2048) -> list[str]:
    """Attach the base-color texture of every material slot (md.textures); returns warnings."""
    from . import texture
    cache, warnings = {}, []
    for md in meshes:
        for slot, mpath in enumerate(md.material_paths):
            if not mpath:
                continue
            if mpath not in cache:
                img = None
                try:
                    img = texture.material_albedo(mpath, max_size)
                except Exception as e:
                    warnings.append(f"{mpath.rsplit('/', 1)[-1]}: {type(e).__name__}: {e}")
                cache[mpath] = img
            if cache[mpath] is not None:
                md.textures[slot] = cache[mpath]
    # The body skin has no color texture (the game shades it in the material): use the average
    # color of the head's skin texture, so body and face match.
    skin = SKIN_COLOR
    for md in meshes:
        if md.name.startswith("Head_") and 0 in md.textures:
            skin = tuple(c / 255.0 for c in md.textures[0].convert("RGB").resize((1, 1)).getpixel((0, 0)))
    for md in meshes:
        for slot, mpath in enumerate(md.material_paths):
            if slot not in md.textures and mpath and "/Bodies/MI_Skin" in mpath:
                md.colors[slot] = skin
    return warnings


SKIN_COLOR = (0.76, 0.58, 0.47)


# ---------- export --------------------------------------------------------------------------
def merged_skeleton(meshes: list[MeshData]):
    """One skeleton for all meshes, joined by bone name (outfit parts each carry the subset of
    the character skeleton they use). -> (names, parents, pose) or None."""
    skel = [m for m in meshes if m.bone_names]
    if not skel:
        return None
    skel.sort(key=lambda m: -len(m.bone_names))
    names, parents, pose, index = [], [], [], {}

    def add(m, i):
        n = m.bone_names[i]
        if n in index:
            return index[n]
        par = m.parents[i]
        pi = add(m, par) if 0 <= par < len(m.bone_names) else -1
        index[n] = len(names)
        names.append(n); parents.append(pi); pose.append(m.pose[i])
        return index[n]
    for m in skel:
        for i in range(len(m.bone_names)):
            add(m, i)
    return names, parents, pose


def item_clips(item: Item):
    """AnimSequence packages for an item (clips of its skeleton; single-skeleton items only)."""
    from . import anim
    if item.category == "characters":
        return []
    try:
        base = weapon_parts(item.key)[0] if "/DA_WeaponVisuals_" in item.key else item.key
        md = load_mesh(base)
    except Exception:
        return []
    skel = getattr(md, "skeleton", None)
    if not skel or skel[0] != "import":
        return []
    return anim.anims_for(skel[1])


def load_clips(item: Item, meshes: list[MeshData]):
    """(decoded clips, warnings) of an item."""
    from . import anim
    clips, warnings = [], []
    gpaths = item_clips(item)
    if not gpaths:
        return clips, warnings
    base = next((m for m in meshes if m.bone_names), None)
    skel = getattr(base, "skeleton", None) if base else None
    if not skel:
        return clips, warnings
    names = anim.skeleton_bones(skel[1])
    pose = None
    for g in gpaths:
        try:
            clip = anim.load_clip(g, names)
        except Exception as e:
            warnings.append(f"{g.rsplit('/', 1)[-1]}: {e}")
            continue
        if clip.additive:               # local-space deltas -> baked onto the bind pose
            if pose is None:
                merged = merged_skeleton(meshes)
                pose = dict(zip(merged[0], merged[2])) if merged else {}
            clip = anim.bake_additive(clip, pose)
        clips.append(clip)
    return clips, warnings


def export_glb(meshes: list[MeshData], path: str, rigged: bool = True, clips=None) -> str:
    """GLB with one node per mesh; material slots with a texture get it embedded as PNG.
    rigged: SkeletalMeshes keep their skeleton and skin weights (static parts follow the bone
    they are attached to). clips: decoded animations (arcpak.anim.Clip) of that skeleton."""
    from . import texture
    glb = gltf.GlbBuilder()
    skin = None
    skel = merged_skeleton(meshes) if rigged else None
    if skel:
        names, parents, pose = skel
        index = {n: i for i, n in enumerate(names)}
        skin, joint_nodes = glb.add_skin(names, parents, pose)
        for clip in clips or []:
            channels = []
            for bone, (trans, rots, scales) in clip.tracks.items():
                if bone not in index:
                    continue
                node = joint_nodes[index[bone]]
                # UE (cm, Z up, left-handed) -> glTF (m, Y up): same mapping as the skeleton
                if trans:
                    channels.append((node, "translation", clip.times(len(trans)),
                                     [(t[0] * 0.01, t[2] * 0.01, t[1] * 0.01) for t in trans]))
                if rots:
                    channels.append((node, "rotation", clip.times(len(rots)),
                                     [(-q[0], -q[2], -q[1], q[3]) for q in rots]))
                if scales:
                    channels.append((node, "scale", clip.times(len(scales)),
                                     [(s[0], s[2], s[1]) for s in scales]))
            glb.add_animation(clip.name, channels)
    for m in meshes:
        for slot, img in m.textures.items():
            name = m.materials[slot] if slot < len(m.materials) else f"mat{slot}"
            glb.textured_material(name, texture.to_png(img))
        for slot, rgb in m.colors.items():
            name = m.materials[slot] if slot < len(m.materials) else f"mat{slot}"
            glb.material(name, tuple(rgb) + (1.0,))
        joints = weights = None
        if skin is not None:
            if m.joints is not None:
                remap = [index[n] for n in m.bone_names]
                joints = [[remap[j] if j < len(remap) else 0 for j in js] for js in m.joints]
                weights = m.weights
            else:
                j = index.get(m.rigid_bone, 0)
                joints = [[j, 0, 0, 0]] * len(m.pos)
                weights = [[1.0, 0.0, 0.0, 0.0]] * len(m.pos)
        gi = glb.add_mesh(m.name, m.pos, m.idx, m.sections, m.nrm, m.uv, mat_names=m.materials,
                          joints=joints, weights=weights)
        glb.add_node(gi, name=m.name, skin=skin)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    glb.write(path)
    return path


def out_path(item: Item) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", item.name).strip("_")
    return os.path.join(OUT_DIR, item.category, safe + ".glb")
