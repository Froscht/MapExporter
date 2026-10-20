"""arcpak.gltf — minimal GLB writer (binary glTF 2.0) for decoded StaticMesh LODs.

Coordinates: UE cm, left-handed, Z up → glTF m, right-handed, Y up: (x, z, y) · 0.01.
Swapping y↔z flips the handedness; the triangle winding stays unchanged (verified against
reference exports: of all candidate mappings, only this one matches).

One mesh per LOD, one primitive per section. Vertices are renumbered per section to
[MinVertexIndex, MaxVertexIndex] (in UE, sections share one vertex buffer; glTF primitives
get their own accessors so that min/max are correct per primitive).
"""
import json, struct


def _pad4(b, fill=b"\0"):
    return b + fill * ((4 - len(b) % 4) % 4)


class GlbBuilder:
    def __init__(self):
        self.bin = bytearray()
        self.views, self.accessors, self.meshes, self.nodes, self.materials = [], [], [], [], []
        self.images, self.textures = [], []
        self.skins, self._children = [], set()
        self.animations = []
        self._mat_index = {}

    def _view(self, data, target):
        while len(self.bin) % 4:
            self.bin.append(0)
        view = {"buffer": 0, "byteOffset": len(self.bin), "byteLength": len(data)}
        if target is not None:                  # image views must not carry a target
            view["target"] = target
        self.views.append(view)
        self.bin += data
        return len(self.views) - 1

    def _acc(self, data, ctype, count, typ, target, mn=None, mx=None):
        a = {"bufferView": self._view(data, target), "componentType": ctype, "count": count, "type": typ}
        if mn is not None:
            a["min"], a["max"] = mn, mx
        self.accessors.append(a)
        return len(self.accessors) - 1

    def material(self, name, color=None):
        """color: optional (r, g, b, a) — base color, double-sided; a < 1 → alphaMode BLEND
        (used for the play-area overlays). Without color, the material carries only its name."""
        if name not in self._mat_index:
            m = {"name": name, "doubleSided": False}
            if color is not None:
                m["doubleSided"] = True
                m["pbrMetallicRoughness"] = {"baseColorFactor": list(color), "metallicFactor": 0.0,
                                             "roughnessFactor": 1.0}
                if color[3] < 1.0:
                    m["alphaMode"] = "BLEND"
            self.materials.append(m)
            self._mat_index[name] = len(self.materials) - 1
        return self._mat_index[name]

    def textured_material(self, name, png: bytes):
        """Material with an embedded PNG as base-color texture (UVs: TEXCOORD_0)."""
        if name not in self._mat_index:
            self.images.append({"bufferView": self._view(png, None), "mimeType": "image/png"})
            self.textures.append({"source": len(self.images) - 1, "sampler": 0})
            self.materials.append({"name": name, "doubleSided": False, "pbrMetallicRoughness": {
                "baseColorTexture": {"index": len(self.textures) - 1}, "metallicFactor": 0.0,
                "roughnessFactor": 0.8}})
            self._mat_index[name] = len(self.materials) - 1
        return self._mat_index[name]

    def add_skin(self, names, parents, pose):
        """Skeleton from a UE reference skeleton: names, parent indices, local (quat, translation,
        scale) per bone in UE coordinates. -> (skin index, [joint node indices])."""
        import numpy as np
        joints = []
        for name, (q, t, s) in zip(names, pose):
            joints.append(len(self.nodes))
            self.nodes.append({"name": name, "rotation": [-q[0], -q[2], -q[1], q[3]],
                               "translation": [t[0] * 0.01, t[2] * 0.01, t[1] * 0.01],
                               "scale": [s[0], s[2], s[1]]})
        world = []
        for i, par in enumerate(parents):
            n = self.nodes[joints[i]]
            x, y, z, w = n["rotation"]
            R = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                          [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                          [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
            M = np.eye(4)
            M[:3, :3] = R * np.array(n["scale"])
            M[:3, 3] = n["translation"]
            world.append(world[par] @ M if 0 <= par < i else M)
            if 0 <= par < len(joints):
                self.nodes[joints[par]].setdefault("children", []).append(joints[i])
                self._children.add(joints[i])
        ibm = b"".join(np.linalg.inv(M).T.astype("<f4").tobytes() for M in world)   # column-major
        acc = self._acc(ibm, 5126, len(world), "MAT4", None)
        self.skins.append({"joints": joints, "inverseBindMatrices": acc})
        return len(self.skins) - 1, joints

    def add_animation(self, name, channels):
        """channels: [(node, "translation" | "rotation" | "scale", times, values)], values already in
        glTF coordinates (vec3 / quaternion xyzw)."""
        samplers, chans = [], []
        for node, path, times, values in channels:
            tdata = struct.pack(f"<{len(times)}f", *times)
            t_acc = self._acc(tdata, 5126, len(times), "SCALAR", None, [min(times)], [max(times)])
            k = 4 if path == "rotation" else 3
            vdata = struct.pack(f"<{k * len(values)}f", *(c for v in values for c in v))
            v_acc = self._acc(vdata, 5126, len(values), "VEC4" if k == 4 else "VEC3", None)
            samplers.append({"input": t_acc, "output": v_acc, "interpolation": "LINEAR"})
            chans.append({"sampler": len(samplers) - 1, "target": {"node": node, "path": path}})
        if chans:
            self.animations.append({"name": name, "samplers": samplers, "channels": chans})

    def add_mesh(self, name, pos, idx, sections, nrm=None, uv=None, mat_names=None, joints=None, weights=None):
        """pos/nrm in UE coordinates (cm), idx = LOD index list, sections = (mat, first, ntri, vmin, vmax).
        joints/weights: optional 4 skin influences per vertex (indices into the skin's joint list)."""
        prims = []
        for mat, first, ntri, vmin, vmax in sections:
            if ntri == 0:
                continue
            sel = range(vmin, vmax + 1)
            pdata = struct.pack(f"<{3 * len(sel)}f", *(c for i in sel for c in
                                                       (pos[i][0] * 0.01, pos[i][2] * 0.01, pos[i][1] * 0.01)))
            # min/max from the float32 values: glTF requires an exact match with the data
            P = list(struct.iter_unpack("<3f", pdata))
            attrs = {"POSITION": self._acc(pdata, 5126, len(P), "VEC3", 34962,
                [min(p[k] for p in P) for k in range(3)], [max(p[k] for p in P) for k in range(3)])}
            if nrm:
                N = []
                for i in sel:
                    x, y, z = nrm[i][0], nrm[i][2], nrm[i][1]
                    L = (x * x + y * y + z * z) ** 0.5
                    N.append((x / L, y / L, z / L) if L > 1e-6 else (0.0, 0.0, 1.0))
                attrs["NORMAL"] = self._acc(struct.pack(f"<{3 * len(N)}f", *(c for n in N for c in n)),
                                            5126, len(N), "VEC3", 34962)
            if joints is not None and weights is not None:
                attrs["JOINTS_0"] = self._acc(struct.pack(f"<{4 * len(sel)}H", *(j for i in sel for j in joints[i])),
                                              5123, len(sel), "VEC4", 34962)
                attrs["WEIGHTS_0"] = self._acc(struct.pack(f"<{4 * len(sel)}f", *(x for i in sel for x in weights[i])),
                                               5126, len(sel), "VEC4", 34962)
            if uv:
                attrs["TEXCOORD_0"] = self._acc(
                    struct.pack(f"<{2 * len(sel)}f", *(c for i in sel for c in uv[i])), 5126, len(sel), "VEC2", 34962)
            I = [i - vmin for i in idx[first:first + 3 * ntri]]
            if max(I) < 65536:
                idata, ct = _pad4(struct.pack(f"<{len(I)}H", *I)), 5123
            else:
                idata, ct = struct.pack(f"<{len(I)}I", *I), 5125
            ia = self._acc(idata, ct, len(I), "SCALAR", 34963)
            prim = {"attributes": attrs, "indices": ia, "mode": 4}
            mname = mat_names[mat] if mat_names and 0 <= mat < len(mat_names) else f"mat{mat}"
            prim["material"] = self.material(mname)
            prims.append(prim)
        self.meshes.append({"name": name, "primitives": prims})
        return len(self.meshes) - 1

    def add_node(self, mesh, name=None, translation=None, rotation=None, scale=None, matrix=None, skin=None):
        """matrix: glTF node matrix (16 floats, column-major; see arcpak.uetransform.to_gltf_matrix).
        glTF forbids translation/rotation/scale when matrix is set."""
        n = {"mesh": mesh}
        if skin is not None:
            n["skin"] = skin
        if name:
            n["name"] = name
        if matrix is not None:
            n["matrix"] = list(matrix)
        else:
            if translation:
                n["translation"] = list(translation)
            if rotation:
                n["rotation"] = list(rotation)
            if scale:
                n["scale"] = list(scale)
        self.nodes.append(n)
        return len(self.nodes) - 1

    def to_bytes(self):
        j = {"asset": {"version": "2.0", "generator": "arcpak.gltf"},
             "buffers": [{"byteLength": len(_pad4(bytes(self.bin)))}],
             "bufferViews": self.views, "accessors": self.accessors, "meshes": self.meshes,
             "nodes": self.nodes, "scene": 0,
             "scenes": [{"nodes": [i for i in range(len(self.nodes)) if i not in self._children]}]}
        if self.materials:
            j["materials"] = self.materials
        if self.skins:
            j["skins"] = self.skins
        if self.animations:
            j["animations"] = self.animations
        if self.images:
            j["images"], j["textures"] = self.images, self.textures
            j["samplers"] = [{"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}]
        js = _pad4(json.dumps(j, separators=(",", ":")).encode(), b" ")
        bn = _pad4(bytes(self.bin))
        total = 12 + 8 + len(js) + 8 + len(bn)
        return (struct.pack("<III", 0x46546C67, 2, total) + struct.pack("<II", len(js), 0x4E4F534A) + js
                + struct.pack("<II", len(bn), 0x004E4942) + bn)

    def write(self, path):
        with open(path, "wb") as f:
            f.write(self.to_bytes())
