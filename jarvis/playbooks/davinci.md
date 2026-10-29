# DaVinci Resolve (video editing, colour, audio, delivery)

DaVinci Resolve is organised as **pages**, switched by the tabs along the very
bottom of the window: Media, Cut, Edit, Fusion, Color, Fairlight, Deliver (and
sometimes an extra page like "Resolve FX"). First find out which page the user
is on with see_screen; the same task is in a different place on each page.

## How to help
- The UI is custom-drawn, so show_steps relies on visible text: page tabs,
  menu names (File, Edit, Trim, Timeline, Clip, Mark, View, Playback,
  Fusion, Color, Fairlight, Workspace, Help), panel headers and button
  labels. Use exact labels. Describe icon-only buttons.
- You cannot see their footage meaning; ask what they want if it is unclear.
- Do not edit their project for them unless asked: point and explain.
  For file-level work on exported media (trim, join, speed, vertical crop,
  captions) use video_tool and transcribe_media instead - they work on the
  files without touching the Resolve project.

## Where things are
- **Import media**: Media page > Media Storage (top-left), double-click or drag
  clips to the Media Pool; or File > Import > Media (`Ctrl/Cmd+I`).
- **Make a timeline**: Edit page > right-click in the Media Pool > Timelines >
  Create New Timeline; or drag a clip into the empty timeline area.
- **Cut and trim (Edit page)**: `B` = Blade tool, `A` = Selection tool; split
  at the playhead with `Cmd+\` (Ctrl+\ on Windows). `Q`/`W` ripple-trim the
  start/end to the playhead; `Shift+Delete` ripple delete.
- **Playback**: `Space` play/pause; `J` `K` `L` reverse/stop/forward; `I` `O`
  mark in/out; arrow keys step a frame.
- **Speed**: right-click the clip > Change Clip Speed (or `Cmd+R` in recent
  versions - confirm the shortcut in Edit > Keyboard Customization if unsure).
- **Titles, transitions, effects**: Effects Library (top-left on Edit page):
  Toolbox > Titles / Video Transitions; drag onto the timeline.
- **Colour**: Color page. Primaries wheels (Lift, Gamma, Gain, Offset) in the
  lower-middle; add a new node with `Alt+S`; scopes via the Scopes button.
- **Audio**: Fairlight page for mixing; on Edit page the Inspector (top-right)
  has Volume under Audio. Loudness: Fairlight > Loudness meter.
- **Subtitles**: Timeline > Create Subtitles from Audio (recent versions) -
  or import an .srt made with transcribe_media: File > Import > Subtitle.
- **Export**: Deliver page > pick a preset (YouTube, H.264 Master…), choose
  folder and filename, then **Add to Render Queue** and **Render All**.
  Quick export: File > Quick Export.

## Frequent problems
- "Media offline": File > Relink Selected Clips (or Media page > right-click >
  Relink) and point to the folder.
- Choppy playback: Playback > Render Cache > Smart or User; or Timeline Proxy
  Mode (Playback menu) to Half Resolution.
- Output looks different: check the timeline colour space and the Deliver
  page's format/codec; export H.264 for sharing.
