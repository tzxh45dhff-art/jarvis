"""
figure_kit — collectible-miniature characters and a photo studio for Blender.

Imported by scripts that run through JARVIS's blender_script tool:

    from figure_kit import figure, studio, render_settings
    studio()                                  # backdrop, lights, camera
    figure({"gender": "female", "skin": "medium", "hair": {"style": "long", "color": "black"},
            "top": {"type": "kurta", "color": "#B33A3A"}, "bottom": {"type": "jeans"},
            "shoes": {"type": "sneakers", "color": "white"}, "pose": "contrapposto"})
    render_settings(quality="final")

Why this exists: a model writing bpy from scratch builds people out of
cylinders and spheres - stick figures. This kit builds one continuous, smooth
body (skin modifier over a posed skeleton, subdivided), a shaped head with
real features, clothes that follow the body, physically based materials, and
a studio that lights and frames it like a product photo of a collectible.
Everything is deterministic from the spec.
"""
from __future__ import annotations

import math
import random

import bmesh
import bpy
from mathutils import Euler, Matrix, Quaternion, Vector

# --------------------------------------------------------------------- palettes

SKIN = {   # sRGB-ish hex; the material converts to linear
    "very_light": "#F3D7C6", "light": "#E8BFA4", "light_medium": "#D9A27F",
    "medium": "#C08562", "tan": "#A8704C", "brown": "#8A5636", "dark": "#62391F",
    "very_dark": "#43271A",
}
HAIR = {"black": "#0D0B0A", "dark_brown": "#2E1F17", "brown": "#5A3A24",
        "light_brown": "#8A6240", "blonde": "#C9A56A", "platinum": "#E3D6BC",
        "red": "#7A2E17", "auburn": "#6B2C1C", "grey": "#8C8A86", "white": "#E8E6E1",
        "blue": "#2C4F9E", "pink": "#D86A9A"}
NAMED = {"white": "#EDEBE6", "black": "#1B1B1D", "navy": "#1F2A44", "blue": "#2F5DA8",
         "denim": "#3B5B86", "light_denim": "#7F9BBE", "red": "#B3312F", "maroon": "#6E1F2A",
         "green": "#2F6B3F", "olive": "#5B6236", "yellow": "#E0B53A", "mustard": "#C9962B",
         "orange": "#D9692B", "pink": "#E59AB4", "purple": "#5E3B8C", "grey": "#7E8188",
         "charcoal": "#36383D", "beige": "#D9C7A7", "khaki": "#B5A27A", "brown": "#6B4A31",
         "tan": "#B08356", "teal": "#2D7D7A", "cream": "#F1E7D2", "gold": "#C7A24A",
         "silver": "#B9BCC2", "saffron": "#E68A2E"}
EYES = {"brown": "#4A2D1A", "dark_brown": "#2B1A10", "hazel": "#6E5A2E", "green": "#3E6B3A",
        "blue": "#3C6E9E", "grey": "#6F7A80", "amber": "#9A6A22"}


def _hex(c, table=NAMED) -> tuple:
    if isinstance(c, (list, tuple)):
        return tuple(c[:3]) + (1.0,)
    c = table.get(str(c).lower(), NAMED.get(str(c).lower(), str(c)))
    c = c.lstrip("#")
    if len(c) != 6:
        c = "808080"
    rgb = [int(c[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in rgb]
    return (*lin, 1.0)


# --------------------------------------------------------------------- materials

def _mat(name, color, rough=0.5, metal=0.0, sss=0.0, sheen=0.0, coat=0.0,
         bump=None, bump_scale=8.0, bump_strength=0.08):
    m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.inputs["Base Color"].default_value = color
    bsdf.inputs["Roughness"].default_value = rough
    bsdf.inputs["Metallic"].default_value = metal
    if sss:
        bsdf.inputs["Subsurface Weight"].default_value = sss
        bsdf.inputs["Subsurface Radius"].default_value = (1.0, 0.35, 0.2)
        bsdf.inputs["Subsurface Scale"].default_value = 0.012
    if sheen:
        bsdf.inputs["Sheen Weight"].default_value = sheen
    if coat:
        bsdf.inputs["Coat Weight"].default_value = coat
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    if bump:
        tex = nt.nodes.new("ShaderNodeTexNoise" if bump != "wave" else "ShaderNodeTexWave")
        if bump == "wave":
            tex.wave_type = "BANDS"
            tex.bands_direction = "Z"
            tex.inputs["Scale"].default_value = bump_scale
            tex.inputs["Distortion"].default_value = 6.0
        else:
            tex.inputs["Scale"].default_value = bump_scale
            tex.inputs["Detail"].default_value = 8.0
        b = nt.nodes.new("ShaderNodeBump")
        b.inputs["Strength"].default_value = bump_strength
        nt.links.new(tex.outputs["Fac"], b.inputs["Height"])
        nt.links.new(b.outputs["Normal"], bsdf.inputs["Normal"])
    return m


def _assign(obj, mat):
    obj.data.materials.clear()
    obj.data.materials.append(mat)


# --------------------------------------------------------------------- helpers

def _link(obj, coll=None):
    (coll or bpy.context.scene.collection).objects.link(obj)
    return obj


def _smooth(obj, level=2, render_level=3):
    for p in obj.data.polygons:
        p.use_smooth = True
    sub = obj.modifiers.new("Subdivision", "SUBSURF")
    sub.levels = level
    sub.render_levels = render_level
    return sub


def _ellipsoid(name, center, radii, rot=(0, 0, 0), segments=32, rings=20, deform=None, basis=None):
    """A UV sphere scaled to radii, optionally reshaped by deform(u, v, w) in
    unit space, then rotated (Euler rot, or a 3x3 basis) and moved to center.
    The transform is baked into the mesh, so shells can be grown off it."""
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=segments, v_segments=rings, radius=1.0)
    R = basis if basis is not None else Euler(rot).to_matrix()
    for v in bm.verts:
        u, vv, w = v.co
        if deform:
            u, vv, w = deform(u, vv, w)
        v.co = R @ Vector((u * radii[0], vv * radii[1], w * radii[2]))
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    ob.location = center
    for p in me.polygons:
        p.use_smooth = True
    return ob


def _apply_transforms(ob):
    mw = ob.matrix_world.copy()
    ob.data.transform(mw)
    ob.matrix_world = Matrix.Identity(4)


# --------------------------------------------------------------------- skeleton

class Skeleton:
    """Joint positions for a posed figure (metres, z up, figure faces -Y)."""

    def __init__(self, H, heads, gender, build, pose):
        self.H, self.heads, self.gender, self.build = H, heads, gender, build
        self.h = H / heads                              # one head height
        fem = gender == "female"
        bw = {"slim": 0.9, "average": 1.0, "athletic": 1.08, "heavy": 1.22}.get(build, 1.0)
        self.bw = bw
        # proportions as fractions of H (realistic adult, adjusted for stylised heads)
        stylise = (7.5 - heads) / 3.0                   # 0 realistic .. 1 chibi-ish
        leg = 0.47 - 0.07 * stylise
        J = {}
        J["pelvis"] = Vector((0, 0, H * (leg + 0.04)))
        h = self.h
        J["chest"] = Vector((0, 0, max(H - 2.0 * h, H * (leg + 0.2))))      # nipple line
        J["waist"] = Vector((0, 0.005 * H, (J["pelvis"].z + J["chest"].z) / 2))
        J["neck"] = Vector((0, 0.01 * H, H - h * 1.1))
        J["head_base"] = Vector((0, 0.01 * H, H - h * 0.72))
        sw = (0.118 if not fem else 0.102) * H * (1 + 0.5 * (bw - 1)) * (1 + 0.15 * stylise)
        hw = (0.085 if fem else 0.078) * H * bw
        J["clav_l"] = J["chest"] + Vector((sw * 0.5, 0.006 * H, h * 0.74))
        J["clav_r"] = J["chest"] + Vector((-sw * 0.5, 0.006 * H, h * 0.74))
        J["shoulder_l"] = Vector((sw, 0.004 * H, J["clav_l"].z - 0.004 * H))
        J["shoulder_r"] = Vector((-sw, 0.004 * H, J["clav_r"].z - 0.004 * H))
        J["hip_l"] = Vector((hw * 0.62, 0, J["pelvis"].z - 0.03 * H))
        J["hip_r"] = Vector((-hw * 0.62, 0, J["pelvis"].z - 0.03 * H))
        self.upper_arm = 0.175 * H * (1 - 0.1 * stylise)
        self.forearm = 0.152 * H * (1 - 0.1 * stylise)
        self.hand = 0.095 * H * (1 - 0.05 * stylise)
        self.thigh = (J["hip_l"].z - 0.045 * H) * 0.52
        self.shin = (J["hip_l"].z - 0.045 * H) * 0.48
        self.J = J
        self.covered = set()                            # body vertex ids under clothes
        self._pose(pose)

    # angles in degrees; arms: abduction from the body, forward swing, elbow bend
    POSES = {
        "stand": dict(arm_abd=(10, 10), arm_fwd=(4, 4), elbow=(14, 14), wrist=(0, 0),
                      leg_abd=(3, 3), knee=(2, 2), hip_shift=0.0, lean=0, head_turn=0, head_tilt=0),
        "contrapposto": dict(arm_abd=(12, 9), arm_fwd=(2, 8), elbow=(18, 22), wrist=(0, 0),
                             leg_abd=(2, 7), knee=(0, 12), hip_shift=0.022, lean=2,
                             head_turn=-8, head_tilt=4),
        "hands_on_hips": dict(arm_abd=(42, 42), arm_fwd=(-6, -6), elbow=(105, 105), wrist=(0, 0),
                              leg_abd=(5, 5), knee=(2, 2), hip_shift=0.0, lean=0,
                              head_turn=0, head_tilt=0),
        "wave": dict(arm_abd=(12, 95), arm_fwd=(4, 10), elbow=(16, 100), wrist=(0, 0),
                     leg_abd=(3, 6), knee=(0, 8), hip_shift=0.012, lean=0,
                     head_turn=6, head_tilt=-3),
        "walking": dict(arm_abd=(9, 9), arm_fwd=(-22, 24), elbow=(20, 30), wrist=(0, 0),
                        leg_abd=(2, 2), knee=(4, 22), hip_shift=0.0, lean=3, head_turn=0,
                        head_tilt=0, stride=(18, -16)),
        "arms_crossed": dict(arm_abd=(14, 14), arm_fwd=(30, 30), elbow=(115, 115), wrist=(0, 0),
                             leg_abd=(4, 4), knee=(0, 4), hip_shift=0.008, lean=-1,
                             head_turn=0, head_tilt=0, cross=True),
        "hero": dict(arm_abd=(20, 20), arm_fwd=(0, 0), elbow=(10, 10), wrist=(0, 0),
                     leg_abd=(9, 9), knee=(0, 0), hip_shift=0.0, lean=-2, head_turn=0,
                     head_tilt=-4, fists=True),
    }

    def _pose(self, pose):
        P = dict(self.POSES.get(pose, self.POSES["stand"]))
        self.pose_name = pose if pose in self.POSES else "stand"
        self.P = P
        J, H = self.J, self.H
        shift = P["hip_shift"] * H
        for k in ("pelvis", "hip_l", "hip_r"):
            J[k] = J[k] + Vector((shift, 0, 0))
        J["waist"] = J["waist"] + Vector((shift * 0.6, 0, 0))
        lean = math.radians(P["lean"])
        for k in ("chest", "clav_l", "clav_r", "shoulder_l", "shoulder_r", "neck", "head_base"):
            v = J[k] - J["waist"]
            J[k] = J["waist"] + Vector((v.x, v.y - math.sin(lean) * v.z, v.z * math.cos(lean)))
        # shoulders tilt opposite to hips in contrapposto
        tilt = -P["hip_shift"] * 0.6 * H
        J["shoulder_l"].z += tilt
        J["shoulder_r"].z -= tilt
        J["clav_l"].z += tilt * 0.5
        J["clav_r"].z -= tilt * 0.5
        # arms
        for side, sgn, i in (("l", 1, 0), ("r", -1, 1)):
            abd = math.radians(P["arm_abd"][i])
            fwd = math.radians(P["arm_fwd"][i])
            d = Vector((sgn * math.sin(abd), -math.sin(fwd) * math.cos(abd),
                        -math.cos(abd) * math.cos(fwd))).normalized()
            el = J[f"shoulder_{side}"] + d * self.upper_arm
            J[f"elbow_{side}"] = el
            bend = math.radians(P["elbow"][i])
            # bend the forearm forward and inward around the elbow
            axis = Vector((1, 0, 0))
            if P.get("cross"):
                fd = Vector((-sgn * 0.85, -0.45, 0.25)).normalized()
            elif self.pose_name == "hands_on_hips":
                fd = Vector((-sgn * 0.55, 0.25, -0.75)).normalized()
            elif self.pose_name == "wave" and side == "r":
                fd = Vector((-0.15, -0.15, 1.0)).normalized()
            else:
                fd = (Matrix.Rotation(-bend, 3, axis) @ d).normalized()
                fd.x += -sgn * 0.08 * math.sin(bend)
                fd.normalize()
            J[f"wrist_{side}"] = el + fd * self.forearm
            J[f"hand_{side}"] = J[f"wrist_{side}"] + fd * self.hand * 0.55
            J[f"_fd_{side}"] = fd
        # legs
        stride = P.get("stride", (0, 0))
        for side, sgn, i in (("l", 1, 0), ("r", -1, 1)):
            abd = math.radians(P["leg_abd"][i])
            st = math.radians(stride[i])
            hip = J[f"hip_{side}"]
            d = Vector((sgn * math.sin(abd), -math.sin(st), -math.cos(abd) * math.cos(st))).normalized()
            knee_bend = math.radians(P["knee"][i])
            kn = hip + d * self.thigh + Vector((0, -math.sin(knee_bend) * self.thigh * 0.35, 0))
            J[f"knee_{side}"] = kn
            ankle = Vector((kn.x + sgn * 0.004 * H, kn.y + math.sin(knee_bend) * 0.15 * self.shin,
                            0.045 * H))
            if self.pose_name == "contrapposto" and side == "r":
                ankle.y -= 0.03 * H
                ankle.x -= 0.01 * H
            J[f"ankle_{side}"] = ankle
            J[f"ball_{side}"] = ankle + Vector((sgn * 0.008 * H, -0.085 * H, -0.03 * H))
            J[f"toe_{side}"] = ankle + Vector((sgn * 0.012 * H, -0.13 * H, -0.036 * H))
            J[f"heel_{side}"] = ankle + Vector((0, 0.025 * H, -0.035 * H))
        # head
        J["head"] = J["head_base"] + Vector((0, -0.014 * H, self.h * 0.22))


# --------------------------------------------------------------------- body via skin modifier

def _skin_mesh(name, verts, edges, radii, root=0):
    """A skin-modifier object: one smooth connected tube network."""
    me = bpy.data.meshes.new(name)
    me.from_pydata([tuple(v) for v in verts], edges, [])
    me.update()
    ob = bpy.data.objects.new(name, me)
    _link(ob)
    mod = ob.modifiers.new("Skin", "SKIN")
    mod.branch_smoothing = 0.6
    mod.use_smooth_shade = True
    skin = me.skin_vertices[0].data
    for i, r in enumerate(radii):
        skin[i].radius = r
        skin[i].use_root = (i == root)
    return ob


def _body(S: Skeleton, fem: bool, chest_scale=1.0):
    J, H, bw = S.J, S.H, S.bw
    names, verts, rad = [], [], []

    def add(n, pos, rx, ry=None):
        names.append(n)
        verts.append(pos)
        rad.append((rx * bw, (ry if ry is not None else rx) * bw))
        return len(verts) - 1

    def mid(a, b, t=0.5):
        return J[a].lerp(J[b], t)

    hip_r = (0.092 if fem else 0.082) * H
    v = {}
    v["pelvis"] = add("pelvis", J["pelvis"], hip_r * 1.02, hip_r * 0.72)
    v["waist"] = add("waist", J["waist"], (0.070 if fem else 0.078) * H, 0.054 * H)
    v["chest"] = add("chest", J["chest"], (0.086 if fem else 0.098) * H,
                     (0.064 if fem else 0.066) * H * chest_scale)
    v["neck"] = add("neck", J["neck"], 0.031 * H, 0.033 * H)
    v["head_base"] = add("head_base", J["head_base"], 0.028 * H, 0.03 * H)
    for s in ("l", "r"):
        v[f"clav_{s}"] = add(f"clav_{s}", J[f"clav_{s}"], 0.05 * H, 0.045 * H)
        v[f"shoulder_{s}"] = add(f"shoulder_{s}", J[f"shoulder_{s}"], 0.038 * H, 0.04 * H)
        v[f"bicep_{s}"] = add(f"bicep_{s}", mid(f"shoulder_{s}", f"elbow_{s}", 0.45),
                              0.034 * H * (0.92 if fem else 1.0), 0.036 * H)
        v[f"elbow_{s}"] = add(f"elbow_{s}", J[f"elbow_{s}"], 0.025 * H, 0.026 * H)
        v[f"forearm_{s}"] = add(f"forearm_{s}", mid(f"elbow_{s}", f"wrist_{s}", 0.35),
                                0.027 * H * (0.9 if fem else 1.0), 0.026 * H)
        v[f"wrist_{s}"] = add(f"wrist_{s}", J[f"wrist_{s}"], 0.018 * H, 0.015 * H)
        v[f"hip_{s}"] = add(f"hip_{s}", J[f"hip_{s}"], 0.064 * H * (1.04 if fem else 1.0),
                            0.064 * H)
        v[f"thigh_{s}"] = add(f"thigh_{s}", mid(f"hip_{s}", f"knee_{s}", 0.45),
                              0.054 * H, 0.056 * H)
        v[f"knee_{s}"] = add(f"knee_{s}", J[f"knee_{s}"], 0.036 * H, 0.038 * H)
        v[f"calf_{s}"] = add(f"calf_{s}", mid(f"knee_{s}", f"ankle_{s}", 0.3) + Vector((0, 0.006 * H, 0)),
                             0.037 * H, 0.042 * H)
        v[f"ankle_{s}"] = add(f"ankle_{s}", J[f"ankle_{s}"], 0.021 * H, 0.022 * H)
        v[f"ball_{s}"] = add(f"ball_{s}", J[f"ball_{s}"], 0.028 * H, 0.016 * H)
        v[f"toe_{s}"] = add(f"toe_{s}", J[f"toe_{s}"], 0.022 * H, 0.012 * H)
    E = [("pelvis", "waist"), ("waist", "chest"), ("chest", "neck"), ("neck", "head_base")]
    for s in ("l", "r"):
        E += [("chest", f"clav_{s}"), (f"clav_{s}", f"shoulder_{s}"),
              (f"shoulder_{s}", f"bicep_{s}"), (f"bicep_{s}", f"elbow_{s}"),
              (f"elbow_{s}", f"forearm_{s}"), (f"forearm_{s}", f"wrist_{s}"),
              ("pelvis", f"hip_{s}"), (f"hip_{s}", f"thigh_{s}"), (f"thigh_{s}", f"knee_{s}"),
              (f"knee_{s}", f"calf_{s}"), (f"calf_{s}", f"ankle_{s}"),
              (f"ankle_{s}", f"ball_{s}"), (f"ball_{s}", f"toe_{s}")]
    edges = [(v[a], v[b]) for a, b in E]
    ob = _meta_body(S, fem, names, verts, edges, rad, chest_scale)
    return ob, v


_META_SURF = 0.723   # a ball chain at spacing 0.23 x radius (stiffness 2) has its surface here
_META_STEP = 0.23


def _meta_body(S, fem, names, verts, edges, rad, chest_scale=1.0):
    """The body as metaballs converted to one watertight mesh. Each body part
    is a dense chain of elements following an anatomical radius profile
    (torso, arms, legs, feet, neck), so limbs taper smoothly, muscles swell
    where they should, and joints blend with no creases or bulges."""
    H, J, bw = S.H, S.J, S.bw
    mb = bpy.data.metaballs.new("BodyMeta")
    mb.threshold = 0.6
    mb.resolution = mb.render_resolution = 0.0038 * H
    mo = bpy.data.objects.new("BodyMeta", mb)
    _link(mo)

    def chain(keys, ellip=False):
        """keys: [(point, rx, ry)] surface radii as fractions of H."""
        pts = [(Vector(p), rx * H * bw, ry * H * bw) for p, rx, ry in keys]
        for (a, ax, ay), (b, bx, by) in zip(pts, pts[1:]):
            seg = b - a
            L = seg.length
            t = 0.0
            while t < L or (b is pts[-1][0] and t <= L + 1e-9):
                u = t / L if L else 1.0
                # smoothstep between keys so radii change gently
                k = u * u * (3 - 2 * u)
                rx, ry = ax + (bx - ax) * k, ay + (by - ay) * k
                e = mb.elements.new()
                e.co = a + seg * u
                e.stiffness = 2.0
                if ellip:
                    e.type = "ELLIPSOID"
                    R = min(rx, ry) / _META_SURF
                    e.radius = R
                    e.size_x, e.size_y, e.size_z = rx / min(rx, ry), ry / min(rx, ry), 1.0
                else:
                    e.type = "BALL"
                    R = (rx + ry) / 2 / _META_SURF
                    e.radius = R
                t += max(_META_STEP * R, 0.0015 * H)
                if L == 0:
                    break

    P, W, C, N = J["pelvis"], J["waist"], J["chest"], J["neck"]
    cs = chest_scale
    if fem:
        hip, waist, chest, top = (0.098, 0.062), (0.072, 0.052), (0.084, 0.06 * cs), (0.074, 0.05)
    else:
        hip, waist, chest, top = (0.088, 0.06), (0.078, 0.055), (0.094, 0.064), (0.088, 0.054)
    chain([(P + Vector((0, 0, -0.052 * H)), 0.05, 0.045),
           (P + Vector((0, 0.002 * H, -0.01 * H)), *hip),
           (P.lerp(W, 0.55), (hip[0] + waist[0]) / 2, (hip[1] + waist[1]) / 2),
           (W, *waist),
           (W.lerp(C, 0.55), (waist[0] + chest[0]) / 2, (waist[1] + chest[1]) / 2),
           (C, *chest),
           (C.lerp(N, 0.45), *top),
           (C.lerp(N, 0.8), 0.045, 0.036)], ellip=True)
    if fem:                                         # bust
        for sgn in (1, -1):
            e = mb.elements.new()
            e.type = "ELLIPSOID"
            e.co = C + Vector((sgn * 0.044 * H, -0.04 * H, -0.004 * H))
            e.radius = 0.036 * H * cs * bw / 0.574
            e.size_x, e.size_y, e.size_z = 1.0, 0.85, 0.95
            e.stiffness = 2.0
    chain([(C.lerp(N, 0.6), 0.032, 0.032), (N, 0.029, 0.03), (J["head_base"], 0.027, 0.028)])
    f = 0.88 if fem else 1.0
    for s in ("l", "r"):
        sh, el, wr = J[f"shoulder_{s}"], J[f"elbow_{s}"], J[f"wrist_{s}"]
        chain([(J[f"clav_{s}"].lerp(N, 0.3), 0.03 * f, 0.03 * f),
               (J[f"clav_{s}"], 0.033 * f, 0.032 * f),
               (sh, 0.037 * f, 0.039 * f),
               (sh.lerp(el, 0.4), 0.033 * f, 0.035 * f),
               (el, 0.024 * f, 0.025 * f),
               (el.lerp(wr, 0.3), 0.027 * f, 0.025 * f),
               (wr, 0.017 * f, 0.016 * f)])
        hp, kn, an = J[f"hip_{s}"], J[f"knee_{s}"], J[f"ankle_{s}"]
        back = Vector((0, 0.008 * H, 0))
        chain([(hp + Vector((0, 0, 0.03 * H)), 0.05, 0.05),
               (hp, 0.06 * (1.04 if fem else 1), 0.062),
               (hp.lerp(kn, 0.3), 0.057 * (1.03 if fem else 1), 0.058),
               (hp.lerp(kn, 0.75), 0.043, 0.044),
               (kn, 0.036, 0.037),
               (kn.lerp(an, 0.28) + back, 0.04, 0.042),
               (kn.lerp(an, 0.7), 0.026, 0.027),
               (an, 0.021, 0.022)])
        chain([(J[f"heel_{s}"] + Vector((0, 0, 0.014 * H)), 0.018, 0.018),
               (an + Vector((0, 0, -0.012 * H)), 0.021, 0.021),
               (J[f"ball_{s}"], 0.02, 0.02),
               (J[f"toe_{s}"], 0.014, 0.014)])
    dg = bpy.context.evaluated_depsgraph_get()
    me = bpy.data.meshes.new_from_object(mo.evaluated_get(dg))
    bpy.data.objects.remove(mo, do_unlink=True)
    bpy.data.metaballs.remove(mb)
    me.name = "Body"
    ob = bpy.data.objects.new("Body", me)
    _link(ob)
    for p in me.polygons:
        p.use_smooth = True
    return ob


def _hand(S: Skeleton, side: str, skin_mat, fists=False):
    """A sculpted hand: palm, a block of four fingers with knuckle grooves, and
    an opposable thumb, oriented to the forearm. At figurine scale this reads
    far better than five loose tubes."""
    J, H = S.J, S.H
    sgn = 1 if side == "l" else -1
    fd = J[f"_fd_{side}"].normalized()
    w = Vector((0, -1, 0))                       # palm width axis: thumb side forward
    w = (w - fd * w.dot(fd))
    if w.length < 1e-3:
        w = Vector((1, 0, 0)) - fd * fd.x
    w.normalize()
    n = fd.cross(w).normalized()                 # palm normal
    if n.x * sgn > 0:                            # palms face the body
        n = -n
    basis = Matrix((w, n, fd)).transposed()      # columns: width, thickness, length
    wr = J[f"wrist_{side}"]
    parts = []
    palm = _ellipsoid(f"Palm_{side}", wr + fd * 0.036 * H, (0.023 * H, 0.0115 * H, 0.032 * H),
                      basis=basis, segments=24, rings=16)
    parts.append(palm)
    curl = 0.55 if fists else 0.12
    fbasis = basis @ Matrix.Rotation(-curl, 3, "X")
    fl = 0.024 if fists else 0.04
    for k in range(4):                           # index .. little finger
        off = (0.75 - k * 0.5) * 0.0108 * H
        ln = (0.93, 1.0, 0.95, 0.8)[k] * fl * H
        c0 = wr + fd * 0.064 * H + w * off + n * (0.003 * H) + (fbasis @ Vector((0, 0, 1))) * ln * 0.6
        parts.append(_ellipsoid(f"Finger{k}_{side}", c0, (0.0058 * H, 0.0062 * H, ln * 0.62),
                                basis=fbasis, segments=12, rings=10))
    tb = basis @ Matrix.Rotation(math.radians(38), 3, "Y") @ Matrix.Rotation(math.radians(-15), 3, "X")
    tc = wr + fd * 0.03 * H + w * 0.02 * H + n * 0.004 * H + (tb @ Vector((0, 0, 1))) * 0.018 * H
    parts.append(_ellipsoid(f"Thumb_{side}", tc, (0.0072 * H, 0.0075 * H, 0.024 * H),
                            basis=tb, segments=12, rings=10))
    for p in parts:
        _link(p)
        _assign(p, skin_mat)
    return parts


# --------------------------------------------------------------------- head and face

def _head_pivot(S: Skeleton):
    """An empty at the head centre carrying the head turn/tilt; face, hair and
    headwear are parented to it with locations relative to the head centre."""
    pv = bpy.data.objects.get("HeadPivot")
    if pv is None:
        pv = bpy.data.objects.new("HeadPivot", None)
        _link(pv)
        pv.location = S.J["head"]
        pv.rotation_euler = (0, math.radians(S.P.get("head_tilt", 0)),
                             math.radians(S.P.get("head_turn", 0)))
    return pv


def _to_head(S, parts):
    pv = _head_pivot(S)
    for p in parts:
        p.parent = pv
    return parts


def _head_radii(h):
    return Vector((h * 0.37, h * 0.45, h * 0.5))


def _skull(u, v, w):
    """Shape a unit sphere into a head: rounder cranium, tapering jaw and chin,
    a flatter face plane, cheekbones, a brow ridge."""
    x, y, z = u, v, w
    if z < 0.05:                                   # jaw taper toward the chin
        t = (0.05 - z) / 1.05
        x *= 1 - 0.34 * t ** 1.3
        if y > 0:
            y *= 1 - 0.45 * t
    if z < -0.55 and y < 0:                        # chin forward a touch
        y -= 0.05 * (-0.55 - z) / 0.45
    if y < -0.5:                                   # flatter face plane
        y = -0.5 - (-y - 0.5) * 0.62
    if y < -0.2 and -0.1 < z < 0.25:               # brow ridge
        y -= 0.035 * (1 - abs(z - 0.12) / 0.15) * max(0.0, 1 - abs(x) / 0.6)
    if z > 0.25 and y > -0.1:                      # fuller back of the skull
        y *= 1.05
    return x, y, z


def _shell(name, src, margin, offset, thick, mat, noise=None, fade=0.12):
    """A layer grown off another mesh (hair, beard). margin(co) is how far a
    vertex is inside the region (<= 0: outside). The layer is full height
    inside and thins to nothing at its edge, like real hair at a hairline,
    so there is no hard ledge."""
    me = src.data.copy()
    ob = bpy.data.objects.new(name, me)
    _link(ob)
    ob.location = src.location
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.normal_update()
    kill, moves = [], []
    for v in bm.verts:
        m = margin(v.co)
        if m <= 0:
            kill.append(v)
        k = max(0.0, min(1.0, m / fade))
        moves.append((v, v.normal.copy(), 0.08 + 0.92 * k * k * (3 - 2 * k)))
    for v, n, k in moves:
        v.co += n * offset * k
    bmesh.ops.delete(bm, geom=kill, context="VERTS")
    loose = [f for f in bm.faces if sum(1 for e in f.edges if e.is_boundary) >= 2]
    bmesh.ops.delete(bm, geom=loose, context="FACES")
    _relax_boundary(bm)
    bm.to_mesh(me)
    bm.free()
    if noise:
        kind, size, strength = noise
        tex = bpy.data.textures.new(name + "Tex", kind)
        tex.noise_scale = size
        d = ob.modifiers.new("Volume", "DISPLACE")
        d.texture = tex
        d.texture_coords = "LOCAL"
        d.strength = strength
        d.mid_level = 0.3
    sol = ob.modifiers.new("Thickness", "SOLIDIFY")
    sol.thickness = thick
    sol.offset = -1
    _smooth(ob, 1, 2)
    _assign(ob, mat)
    return ob


def _head(S: Skeleton, fem: bool, skin_mat, spec):
    h = S.h
    R = _head_radii(h)
    parts = []
    head = _ellipsoid("Head", (0, 0, 0), tuple(R), segments=64, rings=40, deform=_skull)
    _link(head)
    _assign(head, skin_mat)
    _smooth(head, 1, 2)
    parts.append(head)

    eye_z, eye_x = h * 0.02, h * 0.15
    eye_r = h * 0.07
    front = -R.y * 0.81                          # face plane after _skull flattens the front
    iris_col = _hex(spec.get("eyes", "brown"), EYES)
    m_white = _mat("Sclera", _hex("#EFE9E2"), rough=0.12, coat=0.8)
    m_iris = _mat("Iris", iris_col, rough=0.15, coat=1.0)
    m_pupil = _mat("Pupil", _hex("#060606"), rough=0.1, coat=1.0)
    hair_col = _hex((spec.get("hair") or {}).get("color", "dark_brown"), HAIR)
    m_brow = _mat("Brow", hair_col, rough=0.7, bump="noise", bump_scale=300, bump_strength=0.3)
    lip_tone = _hex(spec.get("lips", "#B86A60" if fem else "#A8675A"))
    m_lip = _mat("Lips", lip_tone, rough=0.32, sss=0.3)
    turn_eyes = -math.radians(S.P.get("head_turn", 0)) * 0.6   # eyes look a bit toward camera
    look = Vector((math.sin(turn_eyes) * 0.4, -1, 0)).normalized()
    for sgn in (1, -1):
        ec = Vector((sgn * eye_x, front + eye_r * 0.45, eye_z))
        ball = _ellipsoid(f"Eye_{sgn}", ec, (eye_r, eye_r, eye_r), segments=24, rings=16)
        iris = _ellipsoid(f"Iris_{sgn}", ec + look * eye_r * 0.94, (eye_r * 0.5, eye_r * 0.1, eye_r * 0.5))
        pup = _ellipsoid(f"Pupil_{sgn}", ec + look * eye_r * 1.0, (eye_r * 0.22, eye_r * 0.05, eye_r * 0.22))
        for o, m in ((ball, m_white), (iris, m_iris), (pup, m_pupil)):
            _link(o)
            _assign(o, m)
            parts.append(o)
        # eyelids: upper lid covers the top of the iris, a thin lower lid
        up = _ellipsoid(f"LidUp_{sgn}", ec + Vector((0, -eye_r * 0.06, eye_r * 0.66)),
                        (eye_r * 1.15, eye_r * 1.06, eye_r * 0.5))
        lo = _ellipsoid(f"LidLo_{sgn}", ec + Vector((0, -eye_r * 0.1, -eye_r * 0.74)),
                        (eye_r * 1.05, eye_r * 0.95, eye_r * 0.3))
        for o in (up, lo):
            _link(o)
            _assign(o, skin_mat)
            parts.append(o)
        # lash line
        lash = _ellipsoid(f"Lash_{sgn}", ec + Vector((0, -eye_r * 0.86, eye_r * 0.22)),
                          (eye_r * 0.95, eye_r * 0.12, eye_r * (0.07 if fem else 0.04)))
        _link(lash)
        _assign(lash, m_pupil)
        parts.append(lash)
        brow = _ellipsoid(f"Brow_{sgn}", Vector((sgn * eye_x * 1.04, front - h * 0.025, eye_z + h * 0.12)),
                          (h * 0.088, h * 0.02, h * (0.013 if fem else 0.019)),
                          rot=(0, math.radians(sgn * (-10 if fem else -5)), 0),
                          deform=lambda u, v, w: (u, v, w + 0.5 * (1 - u * u) - 0.25))
        _link(brow)
        _assign(brow, m_brow)
        parts.append(brow)
        ear = _ellipsoid(f"Ear_{sgn}", Vector((sgn * R.x * 0.96, h * 0.03, eye_z - h * 0.04)),
                         (h * 0.03, h * 0.07, h * 0.11), rot=(math.radians(-8), 0, math.radians(sgn * 18)),
                         deform=lambda u, v, w: (u, v, w) if v < 0.2 else (u * 0.4, v, w))
        _link(ear)
        _assign(ear, skin_mat)
        parts.append(ear)
    nose_w = h * (0.042 if fem else 0.05)
    bridge = _ellipsoid("Nose", Vector((0, front - h * 0.022, eye_z - h * 0.07)),
                        (nose_w * 0.6, h * 0.04, h * 0.095), rot=(math.radians(-16), 0, 0))
    tip = _ellipsoid("NoseTip", Vector((0, front - h * 0.055, eye_z - h * 0.15)),
                     (nose_w * 0.8, h * 0.038, h * 0.034))
    for o in (bridge, tip):
        _link(o)
        _assign(o, skin_mat)
        parts.append(o)
    for sgn in (1, -1):
        ala = _ellipsoid(f"Nostril_{sgn}", Vector((sgn * nose_w * 0.7, front - h * 0.03, eye_z - h * 0.16)),
                         (h * 0.02, h * 0.024, h * 0.019))
        _link(ala)
        _assign(ala, skin_mat)
        parts.append(ala)
    smile = {"smile": 0.06, "grin": 0.1, "neutral": 0.0, "serious": -0.03}.get(spec.get("expression", "smile"), 0.04)
    mouth_z = eye_z - h * 0.27
    for name, dz, ry, rz in (("UpperLip", 0.012, 0.022, 0.016), ("LowerLip", -0.016, 0.026, 0.019)):
        lip = _ellipsoid(name, Vector((0, front + h * 0.004, mouth_z + h * dz)),
                         (h * (0.085 if fem else 0.09), h * ry, h * rz),
                         deform=lambda u, v, w, s=smile: (u, v - 0.25 * (1 - u * u), w + s * 6 * u * u))
        _link(lip)
        _assign(lip, m_lip)
        parts.append(lip)
    if spec.get("beard"):
        m_beard = _mat("Beard", hair_col, rough=0.85, bump="noise", bump_scale=500, bump_strength=0.6)
        mz = mouth_z / R.z

        def keep_beard(co):
            u, v, w = co.x / R.x, co.y / R.y, co.z / R.z
            au = abs(u)
            cheek = -0.14 - 0.24 * (1 - au) + max(0.0, min(1.0, (au - 0.7) / 0.12)) * 0.4
            m = min(cheek - w, 0.3 - v)                                     # below the cheek line
            if v < -0.2:                                                    # keep the lips clear
                d = math.hypot(u / 0.34, (w - (mz - 0.03)) / 0.085) - 1
                m = min(m, d * 0.085)
                mo = (1 - math.hypot(u / 0.42, (w - (mz + 0.11)) / 0.055)) * 0.055   # moustache
                m = max(m, mo)
            return m
        beard = _shell("Beard", head, keep_beard, h * 0.016, h * 0.01, m_beard,
                       noise=("CLOUDS", h * 0.03, h * 0.006), fade=0.1)
        parts.append(beard)
    return [_head_pivot(S)] + _to_head(S, parts), head


HAIR_STYLES = ("buzz", "short", "medium", "long", "bob", "bun", "ponytail", "curly", "afro",
               "spiky", "bald")


def _hair(S: Skeleton, spec, head, body=None):
    st = spec.get("hair", {}) or {}
    style = st.get("style", "short")
    if style in ("bald", "none"):
        return []
    h = S.h
    R = _head_radii(h)
    col = _hex(st.get("color", "dark_brown"), HAIR)
    m = _mat("Hair", col, rough=0.62, bump="wave", bump_scale=110, bump_strength=0.5)
    vol = {"buzz": 0.008, "short": 0.045, "medium": 0.07, "long": 0.06, "bob": 0.07, "bun": 0.05,
           "ponytail": 0.04, "curly": 0.1, "afro": 0.22, "spiky": 0.06}.get(style, 0.05) * h
    nape = {"buzz": -0.45, "short": -0.4, "spiky": -0.4, "curly": -0.45, "afro": -0.5}.get(style, -0.98)
    side_low = {"afro": -0.35}.get(style, 0.15)
    if style in ("long", "medium", "bob"):
        nape = -0.35

    def keep(co):
        u, v, w = co.x / R.x, co.y / R.y, co.z / R.z
        if v < -0.25:                                 # front: hairline arc
            line = 0.36 + 0.12 * abs(u) if style != "buzz" else 0.45
            return w - (line - (0.25 if abs(u) > 0.75 else 0))
        if abs(u) > 0.55 and v < 0.25:                # sides and around the ears
            return w - side_low
        return w - nape                               # back
    noise = {"curly": ("VORONOI", h * 0.05, h * 0.04), "afro": ("VORONOI", h * 0.06, h * 0.06),
             "spiky": ("VORONOI", h * 0.09, h * 0.08)}.get(style, ("CLOUDS", h * 0.12, h * 0.02))
    out = [_shell("Hair", head, keep, vol, h * 0.012, m, noise=noise, fade=0.16)]
    if style == "bun":
        out.append(_ellipsoid("Bun", (0, R.y * 0.62, R.z * 0.82), (h * 0.15, h * 0.13, h * 0.13)))
    elif style == "ponytail":
        tail = _skin_mesh("Ponytail", [(0, R.y * 0.95, h * 0.18), (0, R.y * 1.25, -h * 0.25), (0, R.y * 1.1, -h * 0.75)],
                          [(0, 1), (1, 2)], [(h * 0.085, h * 0.085), (h * 0.08, h * 0.07), (h * 0.025, h * 0.025)])
        _bake(tail)
        out.append(tail)
    for o in out[1:]:
        if o.name not in bpy.context.scene.objects:
            _link(o)
        _smooth(o, 1, 2)
        _assign(o, m)
    if style in ("long", "medium", "bob") and body is not None:
        out.append(_hair_curtain(S, body, style, vol * 0.8, m))
    return _to_head(S, out)


def _cloth_mat(name, color, kind):
    if kind == "denim":
        return _mat(name, _hex(color), rough=0.85, sheen=0.3, bump="noise", bump_scale=220, bump_strength=0.18)
    if kind == "leather":
        return _mat(name, _hex(color), rough=0.35, coat=0.25, bump="noise", bump_scale=120, bump_strength=0.05)
    if kind == "knit":
        return _mat(name, _hex(color), rough=0.9, sheen=0.25, bump="noise", bump_scale=160, bump_strength=0.25)
    if kind == "silk":
        return _mat(name, _hex(color), rough=0.3, sheen=0.4)
    return _mat(name, _hex(color), rough=0.78, sheen=0.15, bump="noise", bump_scale=180, bump_strength=0.08)


# Each "bone" of the body with a typical tube radius (fraction of H). Garment
# regions are picked per body vertex by the nearest bone, distance divided by
# that radius, so the side of the chest belongs to the chest, not the arm.
_BONES = [("spine_low", "pelvis", "waist", .085), ("spine_mid", "waist", "chest", .085),
          ("neck", "chest", "neck", .034), ("neck2", "neck", "head_base", .03)]
for _s in ("l", "r"):
    _BONES += [(f"clav_{_s}", "chest", f"clav_{_s}", .05), (f"clav2_{_s}", f"clav_{_s}", f"shoulder_{_s}", .045),
               (f"upper_{_s}", f"shoulder_{_s}", f"elbow_{_s}", .035),
               (f"fore_{_s}", f"elbow_{_s}", f"wrist_{_s}", .026),
               (f"hand_{_s}", f"wrist_{_s}", f"hand_{_s}", .02),
               (f"hip_{_s}", "pelvis", f"hip_{_s}", .07), (f"thigh_{_s}", f"hip_{_s}", f"knee_{_s}", .055),
               (f"shin_{_s}", f"knee_{_s}", f"ankle_{_s}", .038),
               (f"foot_{_s}", f"ankle_{_s}", f"ball_{_s}", .024), (f"toe_{_s}", f"ball_{_s}", f"toe_{_s}", .018)]


def _nearest_bone(S, p, with_point=False):
    best = None
    for n, ja, jb, r in _BONES:
        a, b = S.J[ja], S.J[jb]
        ab = b - a
        L = ab.length_squared
        t = 0.0 if L == 0 else max(0.0, min(1.0, (p - a).dot(ab) / L))
        q = a + ab * t
        d = (q - p).length / (r * S.H)
        if best is None or d < best[0]:
            best = (d, n, t, q)
    name = best[1].rsplit("_", 1)[0] if best[1][-2:] in ("_l", "_r") else best[1]
    return (name, best[2], best[3]) if with_point else (name, best[2])


def _bake(ob, levels=1):
    """Apply the skin (and one subdivision) so the body is a real mesh that
    clothes can be cut from."""
    sub = ob.modifiers.new("BakeSub", "SUBSURF")
    sub.levels = levels
    dg = bpy.context.evaluated_depsgraph_get()
    me = bpy.data.meshes.new_from_object(ob.evaluated_get(dg))
    old = ob.data
    ob.modifiers.clear()
    ob.data = me
    bpy.data.meshes.remove(old)
    bm = bmesh.new()
    bm.from_mesh(me)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(me)
    bm.free()
    for p in me.polygons:
        p.use_smooth = True


def _hide_covered(body, covered, rings=2):
    """Remove body faces that clothes fully cover (shrunk by a couple of rings
    so no gap opens at hems). Skin can then never poke through cloth."""
    bm = bmesh.new()
    bm.from_mesh(body.data)
    bm.verts.ensure_lookup_table()
    inner = set(covered)
    for _ in range(rings):
        inner = {i for i in inner
                 if all(e.other_vert(bm.verts[i]).index in inner for e in bm.verts[i].link_edges)}
    dead = [f for f in bm.faces if all(v.index in inner for v in f.verts)]
    bmesh.ops.delete(bm, geom=dead, context="FACES_ONLY")
    bm.to_mesh(body.data)
    bm.free()


def _relax_boundary(bm, iterations=8):
    """Smooth the cut edge of a shell along itself so hems and hairlines are
    clean curves, not the stair-steps of the source mesh grid."""
    for _ in range(iterations):
        new = {}
        for v in bm.verts:
            be = [e for e in v.link_edges if e.is_boundary]
            if len(be) == 2:
                a, b = (e.other_vert(v).co for e in be)
                new[v] = v.co.z * 0.5 + (a.z + b.z) * 0.25
        for v, z in new.items():
            v.co.z = z


def _loft(name, rings, closed=True):
    """A surface through rings of points (all rings the same length)."""
    n = len(rings[0])
    verts = [tuple(p) for r in rings for p in r]
    faces = []
    for i in range(len(rings) - 1):
        for j in range(n if closed else n - 1):
            a, b = i * n + j, i * n + (j + 1) % n
            faces.append((a, b, b + n, a + n))
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    me.update()
    ob = bpy.data.objects.new(name, me)
    _link(ob)
    for p in me.polygons:
        p.use_smooth = True
    return ob


def _body_reach(S, body, z, center, angles, band=0.025, skip=("upper", "fore", "hand")):
    """How far the body reaches from center (xy) at height z along each angle
    (radians from +x), ignoring the arms. Used to fit skirts and hair."""
    cls = _classify(S, body)
    seg = len(angles)
    best = [0.0] * seg
    step = 2 * math.pi / seg if seg > 1 else 1.0
    for i, v in enumerate(body.data.vertices):
        co = v.co
        if abs(co.z - z) > band * S.H or cls[i][0] in skip:
            continue
        d = co.xy - center
        r = d.length
        if r < 1e-6:
            continue
        a = math.atan2(d.y, d.x)
        for k, ak in enumerate(angles):
            diff = abs((a - ak + math.pi) % (2 * math.pi) - math.pi)
            if diff < step * 0.75 and r * math.cos(diff) > best[k]:
                best[k] = r * math.cos(diff)
    return best


def _classify(S, body):
    """Nearest bone for every body vertex, computed once per figure."""
    if getattr(S, "_cls_n", -1) != len(body.data.vertices):
        S._cls = [_nearest_bone(S, v.co, with_point=True) for v in body.data.vertices]
        S._cls_n = len(body.data.vertices)
    return S._cls


def _skirt(name, S, body, top_z, bot_z, flare, mat, ease=0.014, folds=7):
    """A skirt/kurta/gown panel fitted to the hips and legs, then flared and
    given soft folds toward the hem."""
    H = S.H
    c = S.J["pelvis"].xy
    seg, nr = 64, 14
    angles = [2 * math.pi * k / seg for k in range(seg)]
    radii, rings = None, []
    for i in range(nr + 1):
        t = i / nr
        z = top_z + (bot_z - top_z) * t
        reach = _body_reach(S, body, z, c, angles)
        for _ in range(3):                                   # smooth around the ring
            reach = [(reach[k - 1] + 2 * reach[k] + reach[(k + 1) % seg]) / 4 for k in range(seg)]
        e = ease * min(1.0, 0.45 + t * 2.0)                 # snug at the waist seam, easing out below
        reach = [r + e * H for r in reach]
        radii = reach if radii is None else [max(a, b) for a, b in zip(radii, reach)]   # cloth only falls outward
        f = 1 + (flare - 1) * t ** 1.4
        amp = 0.006 * H * t ** 1.2 * flare
        ring = []
        for k, a in enumerate(angles):
            r = radii[k] * f + amp * math.sin(folds * a + 0.7)
            ring.append(Vector((c.x + r * math.cos(a), c.y + r * math.sin(a), z)))
        rings.append(ring)
    ob = _loft(name, rings)
    sol = ob.modifiers.new("Thickness", "SOLIDIFY")
    sol.thickness = 0.004 * H
    sol.offset = 1
    _smooth(ob, 1, 2)
    _assign(ob, mat)
    return ob


def _hair_curtain(S, body, style, vol, mat):
    """Long/medium/bob hair: a sheet that falls from the crown behind the
    ears down the back, fitted around the neck and shoulders."""
    H, h = S.H, S.h
    R = _head_radii(h)
    hc = S.J["head"]
    length = {"long": 1.75, "medium": 1.05, "bob": 0.78}[style]
    nr, n = 16, 40
    rings = []
    prev = None
    span = math.radians({"bob": 128, "medium": 120, "long": 118}[style])
    angs = [math.pi / 2 - span + 2 * span * j / (n - 1) for j in range(n)]   # around the back (+y)
    side = [((a - math.pi / 2) / span) ** 2 for a in angs]                 # 0 at the back, 1 at the front edge
    ctr = hc.xy
    for i in range(nr + 1):
        t = i / nr
        # a U-shaped hem: the front locks are a little shorter than the back
        zs = [hc.z + 0.12 * h - (length + 0.12) * h * t * (1 - 0.22 * sd) for sd in side]
        head_r = [1 / math.sqrt((math.cos(a) / R.x) ** 2 + (math.sin(a) / R.y) ** 2) + vol for a in angs]
        if zs[n // 2] > hc.z - 0.45 * h:                 # beside the head: follow the skull
            ring_r = head_r
        else:
            reach = [_body_reach(S, body, z, ctr, [a], band=0.03)[0] for z, a in zip(zs, angs)]
            ring_r = [r + 0.014 * H for r in reach]
        if prev:
            # hair hangs: it settles in slowly and spreads over the shoulders gradually
            ring_r = [min(max(r, p * 0.985), p * 1.12) for r, p in zip(ring_r, prev)]
        if style == "bob" and t > 0.7:
            ring_r = [r * (1 + 0.25 * (t - 0.7)) for r in ring_r]
        prev = ring_r
        rings.append([Vector((ctr.x + r * math.cos(a), ctr.y + r * math.sin(a), z)) - hc
                      for r, a, z in zip(ring_r, angs, zs)])
    ob = _loft("HairCurtain", rings, closed=False)
    sol = ob.modifiers.new("Thickness", "SOLIDIFY")
    sol.thickness = 0.02 * h
    sol.offset = -1
    tex = bpy.data.textures.new("HairStrands", "WOOD")
    tex.wood_type = "BANDNOISE"
    tex.noise_scale = 0.4
    d = ob.modifiers.new("Strands", "DISPLACE")
    d.texture = tex
    d.strength = 0.012 * h
    _smooth(ob, 1, 2)
    _assign(ob, mat)
    return ob


def _garment(name, S: Skeleton, body, keep, offset, mat, thick=None):
    """A garment cut from the body's own surface: the faces whose vertices
    pass keep(bone, t, co), pushed out along the normal and given thickness.
    It follows the body exactly, so nothing pokes through."""
    me = body.data.copy()
    ob = bpy.data.objects.new(name, me)
    _link(ob)
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.normal_update()
    kill, moves = [], []
    cls = _classify(S, body)
    for v in bm.verts:
        bone, t, axis_pt = cls[v.index]
        if not keep(bone, t, v.co):
            kill.append(v)
        # push out along the normal, but never inward: the skin modifier can
        # flip normals at branch joints, so trust the bone's radial direction
        radial = v.co - axis_pt
        radial = radial.normalized() if radial.length > 1e-6 else v.normal
        n = v.normal
        d = n if n.dot(radial) > 0.0 else radial
        moves.append((v, d))
    killed = {v.index for v in kill}
    for _ in range(2):                    # remove stray single vertices from the cut
        flip_in, flip_out = [], []
        for v in bm.verts:
            nb = [e.other_vert(v).index in killed for e in v.link_edges]
            if not nb:
                continue
            if v.index in killed and sum(nb) <= len(nb) * 0.25:
                flip_in.append(v.index)
            elif v.index not in killed and sum(nb) >= len(nb) * 0.75:
                flip_out.append(v.index)
        killed.difference_update(flip_in)
        killed.update(flip_out)
    kill = [v for v in bm.verts if v.index in killed]
    S.covered.update(v.index for v in bm.verts if v.index not in killed)
    dirs = {}
    for v, d in moves:
        v.co += d * offset * S.H
        dirs[v] = d
    bmesh.ops.delete(bm, geom=kill, context="VERTS")
    # drop slivers left at the cut so hems are clean
    loose = [f for f in bm.faces if sum(1 for e in f.edges if e.is_boundary) >= 2]
    bmesh.ops.delete(bm, geom=loose, context="FACES")
    # close pinholes left by a stray vertex inside the region (hems are long loops)
    bmesh.ops.holes_fill(bm, edges=[e for e in bm.edges if e.is_boundary], sides=14)
    # hems, cuffs and collars hug the body: pull the last few rings back in
    ring = {v for v in bm.verts if any(e.is_boundary for e in v.link_edges)}
    seen = set(ring)
    for hug in (0.7, 0.45, 0.25, 0.1):
        for v in ring:
            v.co -= dirs.get(v, v.normal) * offset * S.H * hug
        nxt = {e.other_vert(v) for v in ring for e in v.link_edges} - seen
        seen |= nxt
        ring = nxt
    _relax_boundary(bm)
    bm.to_mesh(me)
    bm.free()
    sol = ob.modifiers.new("Thickness", "SOLIDIFY")
    sol.thickness = (thick if thick is not None else 0.0045) * S.H
    sol.offset = -1
    _smooth(ob, 0, 1)
    _assign(ob, mat)
    return ob


def _clothes(S: Skeleton, body, spec, fem):
    out = []
    J, H = S.J, S.H
    top = spec.get("top", {"type": "tshirt", "color": "white"}) or {}
    bottom = spec.get("bottom", {"type": "jeans", "color": "denim"}) or {}
    shoes = spec.get("shoes", {"type": "sneakers", "color": "white"}) or {}
    tt = top.get("type", "tshirt")
    bt = bottom.get("type", "jeans")
    kind = {"hoodie": "knit", "sweater": "knit", "turtleneck": "knit",
            "jacket": "leather" if top.get("material") == "leather" else "cotton"}.get(tt, "cotton")
    m_top = _cloth_mat("Top", top.get("color", "white"), kind)
    hem = J["pelvis"].z - {"tshirt": 0.035, "polo": 0.035, "tank": 0.03, "shirt": 0.05, "hoodie": 0.06,
                           "sweater": 0.05, "turtleneck": 0.05, "jacket": 0.07, "kurta": 0.0,
                           "dress": -0.06, "gown": -0.06, "coat": 0.0}.get(tt, 0.04) * H
    sleeve = {"tshirt": ("upper", 0.5), "polo": ("upper", 0.5), "tank": None,
              "dress": ("upper", 0.3) if top.get("sleeves", "short") == "short" else None,
              "gown": None}.get(tt, ("fore", 0.93))
    neck_t = 0.85 if tt == "turtleneck" else 0.5 if tt == "hoodie" else (0.45 if tt in ("shirt", "polo", "jacket", "coat") else 0.22)

    collar_z = J["neck"].z + {"turtleneck": 0.06, "hoodie": 0.0, "shirt": 0.008, "polo": 0.008,
                              "jacket": 0.012, "coat": 0.02, "tank": -0.05, "dress": -0.03,
                              "gown": -0.04}.get(tt, -0.012) * H
    neck_xy = J["neck"].xy

    def keep_top(bone, t, co):
        if bone in ("thigh", "shin", "foot", "toe"):
            return False
        if bone == "neck2":
            return False
        if bone in ("upper", "fore", "hand"):
            if sleeve is None:
                return bone == "upper" and t < 0.12 and tt != "tank"
            if bone == "upper":
                return sleeve[0] == "fore" or t <= sleeve[1]
            return bone == "fore" and sleeve[0] == "fore" and t <= sleeve[1]
        if tt == "tank" and bone == "clav2" and t > 0.5:
            return False
        if (co.xy - neck_xy).length < 0.05 * H and co.z > collar_z:
            return False
        return co.z >= hem

    top_off = {"jacket": 0.013, "coat": 0.015, "hoodie": 0.01, "sweater": 0.009}.get(tt, 0.007)
    out.append(_garment("Top", S, body, keep_top, top_off, m_top))
    if tt in ("jacket", "coat") and top.get("inner"):
        m_in = _cloth_mat("Inner", top["inner"], "cotton")
        out.append(_garment("Inner", S, body,
                            lambda b, t, co: (b in ("spine_low", "spine_mid", "clav") and co.z >= hem + 0.02 * H)
                            or (b == "neck" and t < 0.3), 0.008, m_in))
    if tt in ("kurta", "dress", "gown", "coat"):
        top_z = J["pelvis"].z + 0.045 * H
        bot_z = {"kurta": J["knee_l"].z + 0.03 * H, "dress": J["knee_l"].z - 0.02 * H,
                 "gown": 0.012 * H, "coat": J["knee_l"].z - 0.01 * H}[tt]
        flare = {"kurta": 1.14, "dress": 1.3, "gown": 1.45, "coat": 1.12}[tt]
        out.append(_skirt(f"{tt.title()}Skirt", S, body, top_z, bot_z, flare, m_top, ease=0.016))
    if tt not in ("dress", "gown"):
        kind_b = "denim" if bt == "jeans" else "cotton"
        m_b = _cloth_mat("Bottom", bottom.get("color", "denim" if bt == "jeans" else "charcoal"), kind_b)
        belt = J["pelvis"].z + 0.05 * H
        if bt == "skirt":
            out.append(_skirt("Skirt", S, body, belt, J["knee_l"].z + 0.03 * H, 1.25, m_b, ease=0.01))
        else:
            leg_end = {"shorts": ("thigh", 0.6), "joggers": ("shin", 0.9)}.get(bt, ("shin", 0.94))

            def keep_bottom(bone, t, co):
                if bone == "thigh":
                    return leg_end[0] == "shin" or t <= leg_end[1]
                if bone == "shin":
                    return leg_end[0] == "shin" and t <= leg_end[1]
                if bone in ("foot", "toe", "upper", "fore", "hand", "neck", "neck2"):
                    return False
                return co.z <= belt
            out.append(_garment("Bottom", S, body, keep_bottom, 0.006 if bt != "joggers" else 0.009, m_b))
    # shoes
    st = shoes.get("type", "sneakers")
    if st not in ("barefoot", "none"):
        kind_s = "leather" if st in ("boots", "formal", "loafers") else "cotton"
        m_s = _cloth_mat("Shoes", shoes.get("color", "white"), kind_s)
        top_t = {"boots": 0.55, "sandals": 1.1}.get(st, 0.86)
        out.append(_garment("Shoes", S, body,
                            lambda b, t, co: b in ("foot", "toe") or (b == "shin" and t >= top_t),
                            0.016 if st != "sandals" else 0.004, m_s, thick=0.006))
        sole_col = shoes.get("sole", "white" if st == "sneakers" else "#2A2522")
        m_sole = _mat("Sole", _hex(sole_col), rough=0.6)
        for s in ("l", "r"):
            a, t = J[f"ankle_{s}"], J[f"toe_{s}"]
            mid = (a + t) / 2
            sole = _ellipsoid(f"Sole_{s}", (mid.x, mid.y + 0.006 * H, 0.008 * H),
                              (0.036 * H, 0.098 * H, 0.009 * H),
                              rot=(0, 0, -math.atan2(t.x - a.x, -(t.y - a.y))))
            _link(sole)
            _assign(sole, m_sole)
            out.append(sole)
    # accessories
    acc = spec.get("accessories", []) or []
    if "glasses" in acc:
        out += _glasses(S)
    if "cap" in acc:
        out += _cap(S, spec)
    if "watch" in acc:
        J, H = S.J, S.H
        w = _ellipsoid("Watch", J["wrist_l"], (0.022 * H, 0.022 * H, 0.012 * H))
        _link(w)
        _assign(w, _mat("Watch", _hex("silver"), rough=0.2, metal=1.0))
        out.append(w)
    if "backpack" in acc:
        J, H = S.J, S.H
        bp = _ellipsoid("Backpack", J["chest"] + Vector((0, 0.11 * H, -0.04 * H)),
                        (0.08 * H, 0.045 * H, 0.11 * H))
        _link(bp)
        _smooth(bp, 1, 2)
        _assign(bp, _cloth_mat("Backpack", spec.get("backpack_color", "charcoal"), "cotton"))
        out.append(bp)
    return out


def _glasses(S):
    h = S.h
    m = _mat("Frames", _hex("#1A1A1A"), rough=0.3, coat=0.6)
    lens = _mat("Lens", _hex("#DDE6EE"), rough=0.0)
    lens.node_tree.nodes["Principled BSDF"].inputs["Transmission Weight"].default_value = 1.0
    parts = []
    front = -h * 0.43 * 0.8 - h * 0.05
    for sgn in (1, -1):
        bpy.ops.mesh.primitive_torus_add(major_radius=h * 0.085, minor_radius=h * 0.008,
                                         location=(sgn * h * 0.145, front, h * 0.03),
                                         rotation=(math.radians(90), 0, 0))
        f = bpy.context.active_object
        _assign(f, m)
        parts.append(f)
    bpy.ops.mesh.primitive_cylinder_add(radius=h * 0.006, depth=h * 0.07,
                                        location=(0, front, h * 0.05),
                                        rotation=(0, math.radians(90), 0))
    br = bpy.context.active_object
    _assign(br, m)
    parts.append(br)
    return _to_head(S, parts)


def _cap(S, spec):
    h = S.h
    col = spec.get("cap_color", "navy")
    m = _cloth_mat("Cap", col, "cotton")
    crown = _ellipsoid("CapCrown", (0, 0, h * 0.16), (h * 0.4, h * 0.46, h * 0.36))
    bm = bmesh.new()
    bm.from_mesh(crown.data)
    bmesh.ops.delete(bm, geom=[v for v in bm.verts if v.co.z < -0.1 * h * 0.36], context="VERTS")
    bm.to_mesh(crown.data)
    bm.free()
    _link(crown)
    s = crown.modifiers.new("Solidify", "SOLIDIFY")
    s.thickness = h * 0.02
    _smooth(crown, 2, 2)
    _assign(crown, m)
    brim = _ellipsoid("CapBrim", (0, -h * 0.42, h * 0.13), (h * 0.27, h * 0.24, h * 0.012))
    _link(brim)
    _assign(brim, m)
    return _to_head(S, [crown, brim])


# --------------------------------------------------------------------- public API

def figure(spec: dict | None = None, height: float = 1.75, location=(0, 0, 0), base=True):
    """
    Build one character. spec keys (all optional):
      gender: "male" | "female"            build: "slim" | "average" | "athletic" | "heavy"
      style:  "realistic" (7.5 heads) | "figure" (6.5) | "stylized" (5.5) | "chibi" (3.5)
      skin:   very_light, light, light_medium, medium, tan, brown, dark, very_dark or "#hex"
      eyes:   brown, dark_brown, hazel, green, blue, grey, amber
      expression: smile | grin | neutral | serious      beard: true/false
      hair:   {"style": buzz|short|medium|long|bob|bun|ponytail|curly|afro|spiky|bald,
               "color": black|dark_brown|brown|light_brown|blonde|red|auburn|grey|white|"#hex"}
      top:    {"type": tshirt|polo|shirt|hoodie|sweater|jacket|kurta|tank|dress|gown|coat,
               "color": name or "#hex", "material": "leather" (jackets), "inner": color}
      bottom: {"type": jeans|trousers|joggers|shorts|skirt, "color": ...}
      shoes:  {"type": sneakers|boots|formal|loafers|sandals|barefoot, "color": ..., "sole": ...}
      accessories: ["glasses", "cap", "watch", "backpack"]   cap_color, backpack_color
      pose:   stand | contrapposto | hands_on_hips | wave | walking | arms_crossed | hero
    Returns the parent empty; the figure stands on a display base when base=True.
    """
    spec = dict(spec or {})
    fem = spec.get("gender", "male") == "female"
    heads = {"realistic": 7.5, "figure": 6.8, "stylized": 5.8, "chibi": 3.6}.get(spec.get("style", "figure"), 6.8)
    S = Skeleton(height, heads, "female" if fem else "male", spec.get("build", "average"),
                 spec.get("pose", "contrapposto"))
    skin_col = _hex(spec.get("skin", "medium"), SKIN)
    m_skin = _mat("Skin", skin_col, rough=0.48, sss=0.25, bump="noise", bump_scale=400, bump_strength=0.03)
    body, body_v = _body(S, fem, chest_scale=1.12 if fem else 1.0)
    _assign(body, m_skin)
    objs = [body]
    for s in ("l", "r"):
        for f in _hand(S, s, m_skin, fists=S.P.get("fists", False)):
            _smooth(f, 1, 2)
            objs.append(f)
    head_parts, head = _head(S, fem, m_skin, spec)
    objs += head_parts
    objs += _hair(S, spec, head, body)
    objs += _clothes(S, body, spec, fem)
    _hide_covered(body, S.covered)
    root = bpy.data.objects.new("Figure", None)
    _link(root)
    lift = 0.0
    if base:
        lift = height * 0.035
        objs.append(_display_base(height, lift))
    for o in objs:
        if o.parent is None:
            o.parent = root
            if o.name != "DisplayBase":
                o.location = o.location + Vector((0, 0, lift))
    root.location = location
    # Where things are, in world coordinates, for props: a bat in the right
    # hand is placed at fig["hand_r"], a hat at fig["head_top"], and so on.
    off = Vector(location) + Vector((0, 0, lift))
    J = S.J
    anchors = {
        "hand_l": J["hand_l"], "hand_r": J["hand_r"],
        "palm_dir_l": J["_fd_l"], "palm_dir_r": J["_fd_r"],
        "foot_l": J["ball_l"], "foot_r": J["ball_r"],
        "head_top": J["head"] + Vector((0, 0, S.h * 0.55)), "face_front": J["head"] + Vector((0, -S.h * 0.45, 0)),
        "chest_front": J["chest"] + Vector((0, -0.07 * height, 0)), "back": J["chest"] + Vector((0, 0.08 * height, 0)),
        "pelvis": J["pelvis"], "base_top": Vector((0, 0, 0)),
    }
    for k, v in anchors.items():
        root[k] = list((v + off) if not k.startswith("palm_dir") else v)
    root["height"] = height
    return root


def _display_base(H, thickness):
    bpy.ops.mesh.primitive_cylinder_add(vertices=96, radius=H * 0.24, depth=thickness,
                                        location=(0, 0, thickness / 2))
    b = bpy.context.active_object
    b.name = "DisplayBase"
    bev = b.modifiers.new("Bevel", "BEVEL")
    bev.width = thickness * 0.25
    bev.segments = 4
    for p in b.data.polygons:
        p.use_smooth = True
    _assign(b, _mat("Base", _hex("#121214"), rough=0.22, coat=0.8))
    return b


def studio(subject_height: float = 1.75, backdrop: str = "#B8B1A7", mood: str = "product",
           camera_angle: str = "three_quarter"):
    """Seamless backdrop, three-point soft lighting, a portrait camera with depth of field."""
    sc = bpy.context.scene
    H = subject_height
    # clear the default scene
    for o in list(sc.objects):
        if o.type in ("MESH", "LIGHT", "CAMERA") and o.name in ("Cube", "Light", "Camera"):
            bpy.data.objects.remove(o, do_unlink=True)
    # cyclorama: floor curving up into a wall
    verts, faces = [], []
    W, D = H * 6, H * 4
    prof = []
    for i in range(0, 12):
        t = i / 11
        ang = t * math.pi / 2
        r = H * 1.2
        prof.append((D * 0.5 - r + math.sin(ang) * r, (1 - math.cos(ang)) * r))
    prof = [(-D * 0.6, 0)] + prof + [(D * 0.5, H * 4)]
    for x in (-W / 2, W / 2):
        for y, z in prof:
            verts.append((x, y, z))
    n = len(prof)
    for i in range(n - 1):
        faces.append((i, i + 1, n + i + 1, n + i))
    me = bpy.data.meshes.new("Backdrop")
    me.from_pydata(verts, [], faces)
    bd = bpy.data.objects.new("Backdrop", me)
    _link(bd)
    for p in me.polygons:
        p.use_smooth = True
    _assign(bd, _mat("Backdrop", _hex(backdrop), rough=0.9))
    # lights
    def area(name, loc, energy, size, color):
        ld = bpy.data.lights.new(name, "AREA")
        ld.energy = energy
        ld.size = size
        ld.color = color
        ob = bpy.data.objects.new(name, ld)
        ob.location = loc
        _link(ob)
        d = Vector((0, 0, H * 0.55)) - Vector(loc)
        ob.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()
        return ob
    k = {"product": 1.0, "dramatic": 1.3, "soft": 0.8}.get(mood, 1.0)
    area("Key", (-H * 1.4, -H * 1.6, H * 1.7), 260 * k * H ** 2, H * 1.3, (1.0, 0.93, 0.85))
    area("Fill", (H * 1.6, -H * 1.2, H * 1.0), 70 * (0.6 if mood == "dramatic" else 1) * H ** 2, H * 1.6, (0.86, 0.92, 1.0))
    area("Rim", (H * 0.6, H * 1.4, H * 1.9), 220 * k * H ** 2, H * 0.8, (1.0, 0.97, 0.92))
    area("Top", (0, 0, H * 2.6), 30 * H ** 2, H * 2.0, (1, 1, 1))
    world = sc.world or bpy.data.worlds.new("World")
    sc.world = world
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.03, 0.03, 0.035, 1)
    world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.15
    # camera
    cd = bpy.data.cameras.new("Camera")
    cd.lens = 85
    cd.dof.use_dof = True
    cd.dof.aperture_fstop = 5.6
    cam = bpy.data.objects.new("Camera", cd)
    _link(cam)
    yaw = {"front": 0, "three_quarter": 28, "side": 80}.get(camera_angle, 28)
    dist = H * 3.5
    target = Vector((0, 0, H * 0.52))
    cam.location = target + Vector((math.sin(math.radians(yaw)) * dist, -math.cos(math.radians(yaw)) * dist, H * 0.22))
    cam.rotation_euler = (target - cam.location).to_track_quat("-Z", "Y").to_euler()
    cd.dof.focus_distance = (target - cam.location).length
    sc.camera = cam
    return cam


def render_settings(quality: str = "preview", width: int = 1080, height: int = 1350):
    """Cycles on the GPU with denoising; 'preview' is fast, 'final' is clean."""
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
        prefs.compute_device_type = "METAL"
        prefs.get_devices()
        for d in prefs.devices:
            d.use = True
        sc.cycles.device = "GPU"
    except Exception:
        pass
    sc.cycles.samples = {"preview": 48, "final": 256}.get(quality, 96)
    sc.cycles.use_denoising = True
    sc.render.resolution_x = width
    sc.render.resolution_y = height
    sc.render.resolution_percentage = 100 if quality == "final" else 60
    try:
        sc.view_settings.view_transform = "AgX"
        sc.view_settings.look = "AgX - Punchy"
    except Exception:
        pass
    sc.render.film_transparent = False
