# Streaming sites (Netflix, YouTube, Prime Video, Hotstar, Spotify web...)

"Play / resume X" is done when the video is **actually playing** - not when the
page has loaded.

## Fast path (3 steps)
1. If the user's browser already has the watch link open (check browser_tabs),
   use that exact URL. A Netflix `/watch/<id>` link **resumes at the saved
   position by itself** while signed in; YouTube remembers it too.
2. browser_open the link. For video pages the result carries `media_playing`:
   - `true` (and `verified: true`) = it is playing. Say so and stop. Do not
     reload, re-open, search again or "double check" - it is confirmed.
   - `false` = it loaded but is not playing yet. Look at the page once
     (browser_read / see_screen). If a profile picker is shown, press the
     user's profile; if a Play / Resume / Continue button is shown, press it.
     Then call video_state once.
3. Report: what is playing, on which site, and whether it resumed (Netflix
   shows the progress bar and a time at the bottom of the player).

## When it is not the saved link
- Search the site once (its own URL, e.g. `https://www.netflix.com/search?q=...`
  or the title page), click Play / Resume, then video_state.
- Never type a password. If a sign-in page appears, tell the user and stop.

## Never
- Loop: if two attempts did not start playback, stop and say exactly what you
  see (sign-in page, profile picker, error code, region block).
- Reopen the same URL "to check". video_state checks without touching the page.
- Claim it resumed at a given minute unless the player's time is visible.
