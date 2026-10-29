# Blender: characters and the figure kit

JARVIS drives Blender headlessly (`blender_script`: Blender's `-b` mode, no window)
for modelling, materials, import/export and rendering.

Left alone, a language model writes a person from cylinders and spheres. The result
is a stick figure. `jarvis/blender/figure_kit.py` is the fix: every script run through
`blender_script` can `from figure_kit import figure, studio, render_settings`, and the
kit builds a sculpted, clothed, lit character in one call (about 7 s to build, 5 s
for a preview render and 25 s for a final one on an M4 Pro, using the Metal GPU).

```python
import bpy
from figure_kit import figure, studio, render_settings

studio(backdrop="#B8B1A7", mood="product", camera_angle="three_quarter")
fig = figure({
    "gender": "female", "build": "average", "skin": "light_medium", "eyes": "brown",
    "hair": {"style": "long", "color": "dark_brown"}, "expression": "smile",
    "top": {"type": "kurta", "color": "#B33A3A"}, "bottom": {"type": "trousers", "color": "cream"},
    "shoes": {"type": "sandals", "color": "tan"}, "pose": "hands_on_hips",
    "accessories": ["watch"],
})
render_settings("final")      # "preview" while iterating
bpy.context.scene.render.filepath = "/tmp/figurine.png"
bpy.ops.render.render(write_still=True)
```

## Options

| | |
|---|---|
| `gender` | `male`, `female` |
| `build` | `slim`, `average`, `athletic`, `heavy` |
| `style` | `realistic` (7.5 heads tall), `figure` (6.8, default), `stylized` (5.8), `chibi` (3.6) |
| `skin` | `very_light`, `light`, `light_medium`, `medium`, `tan`, `brown`, `dark`, `very_dark`, or `#hex` |
| `hair` | style: `buzz short medium long bob bun ponytail curly afro spiky bald`; colour name or `#hex` |
| `beard`, `expression` | `true/false`; `smile grin neutral serious` |
| `top` | `tshirt polo shirt hoodie sweater turtleneck jacket coat kurta tank dress gown` (+ `color`, `inner`) |
| `bottom` | `jeans trousers joggers shorts skirt` |
| `shoes` | `sneakers boots formal loafers sandals barefoot` |
| `accessories` | `glasses cap watch backpack` |
| `pose` | `stand contrapposto hands_on_hips wave walking arms_crossed hero` |

`figure()` returns the figure's root object. Its custom properties give world
positions for placing props: `fig["hand_r"]`, `hand_l`, `palm_dir_r`, `head_top`,
`face_front`, `chest_front`, `back`, `foot_l`, `foot_r`, `base_top`. A held object
goes at the hand and is parented to `fig`; a render shows whether it touches.

## How it is built

The aim is a continuous, organic surface, because that is what separates a figurine
from an assembly of primitives.

- **Skeleton** (`Skeleton`): joint positions from head-count proportions, then posed by
  angles (abduction, swing, elbow bend, stride).
- **Body**: *metaballs*, converted to one watertight mesh. Each limb is a dense chain
  of elements following an anatomical radius profile (thigh → knee → calf → ankle). A
  chain at spacing 0.23 × radius has a predictable surface (0.723 × radius at
  stiffness 2, threshold 0.6, with no ripple), which is how the radii are calibrated.
  Joints blend with no creases. The Skin modifier was tried first and rejected: it
  flips normals and kinks at branches.
- **Head and face**: a deformed sphere (jaw taper, face plane, brow ridge) with eyeballs,
  irises, pupils, lids, lashes, brows, nose, lips, ears; mood sets the smile curve.
- **Clothes**: shells *cut from the body mesh*. Each body vertex is classified by its
  nearest bone; a garment keeps the vertices its rule selects, pushes them out along the
  normal, thickens them, hugs the hem back toward the body and fills pinholes. Because
  they come from the body, they cannot float or poke through. Skirts, kurtas and gowns
  are lofted rings fitted to the hips and legs per angle, with soft folds.
- **Hair and beard**: shells grown off the head whose offset fades to zero at the edge
  (a natural hairline, no ledge). Long hair is a lofted curtain from the head silhouette.
- **Studio**: seamless cyclorama, key/fill/rim/top area lights, an 85 mm camera with depth
  of field; Cycles with denoising and the AgX view transform.

## Known limits

Hands are simple (palm, four fingers, thumb; no individual finger poses). There is no
rig yet, so no animation. Faces are stylised, not portraits of a specific person: to
make a *character*, JARVIS translates their look into the options above (hair, outfit
colours, signature accessory) and adds props with the anchors. Cloth does not
simulate; the drape is modelled.

## How JARVIS uses it

`jarvis/playbooks/blender.md` is loaded into the request when Blender is involved. It
requires the kit for people and a render → `look_at_image` → fix → render loop before
a result is handed back. With `JARVIS_AZURE_BLENDER_DEPLOYMENT` set, these jobs run on
a stronger model (see [model routing](model-routing-and-cost.md)).
