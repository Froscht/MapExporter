"""arcpak.uetransform — Unreal Engine transform math (FRotator, FQuat, FTransform, FMatrix) in pure Python.

Formulas follow Unreal Engine's math (UnrealMath.cpp / TransformVectorized.h):
    FRotator(Pitch, Yaw, Roll) in degrees → FQuat (Rotator::Quaternion)
    FTransform composition: (A * B) = apply A first, then B
        R = B.R · A.R,  S = A.S ⊙ B.S,  T = B.R(B.S ⊙ A.T) + B.T
    Component world transform = Relative * ParentWorld (USceneComponent::UpdateComponentToWorld)
Matrices here use column convention (p' = M p); a UE FMatrix (row vectors, translation in row 3)
is transposed when read.
"""
import math


def rotator_to_quat(pitch, yaw, roll):
    h = math.pi / 360.0
    sp, cp = math.sin(pitch * h), math.cos(pitch * h)
    sy, cy = math.sin(yaw * h), math.cos(yaw * h)
    sr, cr = math.sin(roll * h), math.cos(roll * h)
    return (cr * sp * sy - sr * cp * cy,
            -cr * sp * cy - sr * cp * sy,
            cr * cp * sy - sr * sp * cy,
            cr * cp * cy + sr * sp * sy)


def quat_mul(a, b):
    """a·b (Hamilton product): applies b first, then a."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


def quat_rotate(q, v):
    x, y, z, w = q
    tx = 2 * (y * v[2] - z * v[1])
    ty = 2 * (z * v[0] - x * v[2])
    tz = 2 * (x * v[1] - y * v[0])
    return (v[0] + w * tx + (y * tz - z * ty),
            v[1] + w * ty + (z * tx - x * tz),
            v[2] + w * tz + (x * ty - y * tx))


def quat_matrix(q):
    x, y, z, w = q
    return [[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
            [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
            [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]]


class Transform:
    __slots__ = ("r", "t", "s")

    def __init__(self, r=(0.0, 0.0, 0.0, 1.0), t=(0.0, 0.0, 0.0), s=(1.0, 1.0, 1.0)):
        self.r, self.t, self.s = tuple(r), tuple(t), tuple(s)

    @classmethod
    def from_relative(cls, loc=None, rot=None, scale=None):
        return cls(rotator_to_quat(*rot) if rot else (0.0, 0.0, 0.0, 1.0),
                   loc or (0.0, 0.0, 0.0), scale or (1.0, 1.0, 1.0))

    def __mul__(self, b):
        """self * b: applies self first, then b (UE FTransform::Multiply)."""
        a = self
        r = quat_mul(b.r, a.r)
        s = (a.s[0] * b.s[0], a.s[1] * b.s[1], a.s[2] * b.s[2])
        st = (b.s[0] * a.t[0], b.s[1] * a.t[1], b.s[2] * a.t[2])
        rt = quat_rotate(b.r, st)
        return Transform(r, (rt[0] + b.t[0], rt[1] + b.t[1], rt[2] + b.t[2]), s)

    def matrix(self):
        """4×4 matrix in column convention, including scale (ToMatrixWithScale)."""
        R = quat_matrix(self.r)
        M = [[R[i][j] * self.s[j] for j in range(3)] + [self.t[i]] for i in range(3)]
        return M + [[0.0, 0.0, 0.0, 1.0]]

    def apply(self, p):
        v = quat_rotate(self.r, (p[0] * self.s[0], p[1] * self.s[1], p[2] * self.s[2]))
        return (v[0] + self.t[0], v[1] + self.t[1], v[2] + self.t[2])


def fmatrix_rows_to_cols(m16):
    """UE FMatrix (16 values, row-major, row-vector convention) → 4×4 column convention."""
    return [[m16[c * 4 + r] for c in range(4)] for r in range(4)]


def matmul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def to_gltf_matrix(M):
    """UE column matrix (cm, Z up, left-handed) → glTF node matrix (m, Y up), flattened column-major.
    glTF point = C·p·0.01 with C = swap y↔z; node = C·A·C⁻¹, translation C·t·0.01."""
    P = (0, 2, 1)
    A = [[M[P[i]][P[j]] for j in range(3)] for i in range(3)]
    t = [M[P[i]][3] * 0.01 for i in range(3)]
    return [A[0][0], A[1][0], A[2][0], 0.0,
            A[0][1], A[1][1], A[2][1], 0.0,
            A[0][2], A[1][2], A[2][2], 0.0,
            t[0], t[1], t[2], 1.0]


def _selftest():
    # Yaw 90: X → +Y (UE convention)
    q = rotator_to_quat(0, 90, 0)
    v = quat_rotate(q, (1, 0, 0))
    assert max(abs(a - b) for a, b in zip(v, (0, 1, 0))) < 1e-12, v
    # Pitch 90: X → +Z (nose up)
    v = quat_rotate(rotator_to_quat(90, 0, 0), (1, 0, 0))
    assert max(abs(a - b) for a, b in zip(v, (0, 0, 1))) < 1e-12, v
    # Roll 90: Y → −Z (UE FRotationMatrix, row 1 = (SR·SP·CY − CR·SY, SR·SP·SY + CR·CY, −SR·CP))
    v = quat_rotate(rotator_to_quat(0, 0, 90), (0, 1, 0))
    assert max(abs(a - b) for a, b in zip(v, (0, 0, -1))) < 1e-12, v
    # Quaternion matrix == FRotationMatrix for a general rotator
    P_, Y_, R_ = 17.0, -63.0, 121.0
    r = math.radians
    SP, CP, SY, CY, SR, CR = math.sin(r(P_)), math.cos(r(P_)), math.sin(r(Y_)), math.cos(r(Y_)), math.sin(r(R_)), math.cos(r(R_))
    rows = [(CP * CY, CP * SY, SP),
            (SR * SP * CY - CR * SY, SR * SP * SY + CR * CY, -SR * CP),
            (-(CR * SP * CY + SR * SY), CY * SR - CR * SP * SY, CR * CP)]
    Q = quat_matrix(rotator_to_quat(P_, Y_, R_))
    assert max(abs(Q[i][j] - rows[j][i]) for i in range(3) for j in range(3)) < 1e-12
    # Composition: child (100,0,0) under parent with yaw 90 + (10,20,30) → (10,120,30)
    child = Transform.from_relative((100, 0, 0))
    parent = Transform.from_relative((10, 20, 30), (0, 90, 0))
    w = child * parent
    assert max(abs(a - b) for a, b in zip(w.t, (10, 120, 30))) < 1e-9, w.t
    # Matrix == apply
    p = (3.0, -7.0, 11.0)
    T = Transform.from_relative((5, 6, 7), (10, 20, 30), (2, 3, 4))
    M = T.matrix()
    mp = tuple(sum(M[i][k] * (p + (1.0,))[k] for k in range(4)) for i in range(3))
    assert max(abs(a - b) for a, b in zip(mp, T.apply(p))) < 1e-9
    return "ok"


if __name__ == "__main__":
    print(_selftest())
