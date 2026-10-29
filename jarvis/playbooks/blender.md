# Blender (3D modelling, animation, rendering)

Two ways to help: **teach/point** (the user is at the screen) or **do it
headless** (blender_script runs bpy with no window - modelling, materials,
import/export, rendering, batch jobs). Pick by what was asked: "how do I…" /
"show me" = point; "make/render/convert this" = headless.

## Before you coach
1. see_screen to learn what they are looking at. Blender's workspace tabs sit
   along the top (Layout, Modeling, Sculpting, UV Editing, Texture Paint,
   Shading, Animation, Rendering, Compositing, Geometry Nodes, Scripting).
   The mode dropdown (Object Mode / Edit Mode / Sculpt Mode…) is at the top-left
   of the 3D viewport. Menus: File, Edit, Render, Window, Help; in the viewport
   header: View, Select, Add, Object.
2. Blender is custom-drawn: there is no accessibility tree. show_steps works by
   reading visible text (menu names, tab names, panel headers) - use those exact
   labels. For icon-only buttons describe them ("the camera icon in the
   Properties editor's tab column").

## Keys that matter (3D viewport, default keymap)
- Navigate: middle mouse = orbit; Shift+middle = pan; scroll = zoom; numpad
  `.` = frame selected; numpad 1/3/7 = front/right/top; numpad 0 = camera view.
- Select: click; `A` all; `Alt+A` none; `B` box; `C` circle; `Ctrl+I` invert.
- Transform: `G` move, `R` rotate, `S` scale, then `X`/`Y`/`Z` to lock an axis
  and type a number, Enter to confirm, Esc to cancel.
- Modes: `Tab` object/edit toggle; `Ctrl+Tab` mode pie menu.
- Edit mode: `1/2/3` vertex/edge/face; `E` extrude; `Ctrl+R` loop cut; `I`
  inset; `Ctrl+B` bevel; `M` merge; `F` fill; `X` delete menu.
- Add: `Shift+A`. Duplicate: `Shift+D`. Delete: `X`. Hide: `H`, reveal `Alt+H`.
- Parent: select child, then parent (active), `Ctrl+P`. Apply transforms:
  `Ctrl+A`. Join: `Ctrl+J`. Origin: Object > Set Origin.
- Undo `Ctrl+Z`; redo `Ctrl+Shift+Z`. Save `Ctrl+S`.
- Search any command: `F3`.

## Common tasks (where to click)
- **Render an image**: set the render engine and samples in the Properties
  editor > Render tab; Output tab for resolution; then Render > Render Image
  (`F12`). Save it from the render window: Image > Save As.
- **Render an animation**: set Frame Start/End in the Timeline or Output tab,
  Output tab > file format and folder, then Render > Render Animation
  (`Ctrl+F12`).
- **Add a material**: select the object > Properties > Material tab > New.
  Base Color, Metallic, Roughness are in the Principled BSDF panel.
- **Add a modifier**: Properties > Modifiers tab (wrench icon) > Add Modifier.
  Subdivision Surface is `Ctrl+1/2/3`.
- **Keyframe**: hover the property and press `I`; move the timeline; change
  the value; `I` again. Timeline is at the bottom.
- **Import/export**: File > Import / Export (glTF, FBX, OBJ, USD, STL).
- **Lights and camera**: Add > Light / Camera; `Ctrl+Alt+Numpad 0` snaps the
  camera to the current view.

## Doing it headless (blender_script)
- Blender 5.x Python API. Start with `import bpy`. A factory-startup scene
  already has Cube, Light and Camera - delete or reuse them deliberately.
- Render: pass render_output="/path/out.png"; set scene.render.engine,
  resolution and samples in the script first. Prefer a low sample count for
  previews.
- To change an existing project: pass blend_file, modify, then
  `bpy.ops.wm.save_as_mainfile(filepath=...)` to a NEW path; never overwrite
  the user's .blend without asking.
- The script prints are returned; check them, then verify any file you made.

## People, characters and figurines - always use figure_kit
Never build a person from cylinders and spheres (that is what makes stick
figures). Every blender_script can `from figure_kit import figure, studio,
render_settings`; it builds a sculpted body (anatomical proportions, smooth
joints), a real face (eyes with iris/pupil and lids, brows, nose, lips, ears),
hair, beard, fitted clothes and a display base, then lights it like a product
photo:
```python
import bpy
from figure_kit import figure, studio, render_settings
studio(backdrop="#B8B1A7", mood="product", camera_angle="three_quarter")
figure({"gender": "male", "build": "average", "skin": "medium", "eyes": "brown",
        "hair": {"style": "short", "color": "black"}, "beard": True, "expression": "smile",
        "top": {"type": "hoodie", "color": "olive"}, "bottom": {"type": "jeans"},
        "shoes": {"type": "sneakers", "color": "white"}, "pose": "contrapposto",
        "accessories": ["watch"]})
render_settings("final")          # "preview" while iterating
```
Options (all optional): gender male|female; build slim|average|athletic|heavy;
style realistic|figure|stylized|chibi; skin very_light..very_dark or "#hex";
hair style buzz|short|medium|long|bob|bun|ponytail|curly|afro|spiky|bald;
top tshirt|polo|shirt|hoodie|sweater|turtleneck|jacket|coat|kurta|tank|dress|gown
(+ "inner" colour for jackets); bottom jeans|trousers|joggers|shorts|skirt;
shoes sneakers|boots|formal|loafers|sandals|barefoot; pose stand|contrapposto|
hands_on_hips|wave|walking|arms_crossed|hero; accessories glasses|cap|watch|backpack.
Props go where the body is: `fig = figure(...)` returns an object whose custom
properties give world positions - fig["hand_r"], fig["hand_l"] (grip points;
fig["palm_dir_r"] is the direction the fingers point), fig["head_top"],
fig["face_front"], fig["chest_front"], fig["back"], fig["foot_l"/"foot_r"],
fig["base_top"]. Put a held object's grip exactly at the hand and parent it to
`fig`; check in the render that it touches the hand. Keep any text on the base
short enough to fit (measure the text object's dimensions against the base).
To recreate a known character, translate their look into these options (hair,
colours, outfit, signature accessory). Several figures: call figure() with
location=(x, 0, 0) for each and studio(subject_height=...) once.

## Quality loop (required for anything you model)
1. Render a preview with render_output, then look_at_image the PNG yourself.
2. Name what is wrong (proportions, gaps or intersections, flat lighting,
   framing, colour) and fix it in the script; re-render. Repeat until it
   looks like a real product photo - usually 2-3 passes.
3. Then render_settings("final") once. Never hand over an unchecked render.
For non-character models: real-world scale in metres, bevel or subdivide hard
edges, give every object a proper material (roughness, not flat colours), and
use studio() for lighting and camera unless the scene has its own.
