"""
The quick paths: media commands that skip the model, the player a bare "play"
means, exact Spotify links, family words in Contacts, WhatsApp numbers, and
playbooks attached up front. Local only: no model, no network, no audio.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis.core import fastpath
from jarvis.tools import comms, media
from jarvis.tools import playbook as pb

R = []


def check(n, c, e=""):
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else f"\n        {str(e)[:300]}"))
    R.append(bool(c))


# ------------------------------------------------------------ command matcher
print("=== media commands ===")
fastpath._music_context = lambda: True            # pretend a player is in use
want = {
    "play": ("play_music", {}),
    "hey jarvis, play": ("play_music", {}),
    "resume the music": ("play_music", {}),
    "pause": ("control_playback", {"action": "pause"}),
    "stop the music": ("control_playback", {"action": "pause"}),
    "next song": ("control_playback", {"action": "next"}),
    "skip": ("control_playback", {"action": "next"}),
    "previous track": ("control_playback", {"action": "previous"}),
    "go back a song": ("control_playback", {"action": "previous"}),
    "volume up": ("set_volume", {"change": 15}),
    "turn it down": ("set_volume", {"change": -15}),
    "set volume to 30": ("set_volume", {"level": 30}),
    "mute": ("set_volume", {"mute": True}),
    "play flashing lights on spotify": ("play_music", {"query": "flashing lights", "app": "Spotify"}),
    "play the album starboy": ("play_music", {"query": "starboy", "kind": "album"}),
}
for text, exp in want.items():
    got = fastpath.action(text)
    check(f"{text!r} runs directly", got == exp, got)
for text in ["play the mentalist on netflix", "play iron man", "go back", "back",
             "pause my download", "stop the timer", "next week's weather",
             "play some music", "set volume to 300",
             "play the movie interstellar by christopher nolan"]:
    check(f"{text!r} goes to the model", fastpath.action(text) is None, fastpath.action(text))

fastpath._music_context = lambda: False           # a video is in front
check("bare 'pause' with a video in front goes to the model", fastpath.action("pause") is None)
check("'play X on spotify' still runs with a video in front",
      fastpath.action("play tum hi ho on spotify") is not None)

check("reply names the song",
      fastpath.reply("play_music", {}, {"title": "Flashing Lights", "artist": "Kanye West",
                                        "app": "Spotify"}) == "Playing Flashing Lights by Kanye West on Spotify.")
check("reply for pause", fastpath.reply("control_playback", {"action": "pause"}, {}) == "Paused.")
check("reply for volume", fastpath.reply("set_volume", {"level": 30}, {"volume": 30}) == "Volume 30%.")

# ------------------------------------------------------------ which player
print("\n=== which player a bare 'play' means ===")
import jarvis.context as ctx
real_fw, real_players, real_wq = ctx.front_window, ctx.players, ctx._windows_quartz


def scene(front, states, order=()):
    ctx.front_window = lambda: (front, "")
    ctx.players = lambda: [{"app": a, "state": s, "title": "t", "artist": "a"} for a, s in states.items()]
    ctx._windows_quartz = lambda: [(a, "") for a in order]


scene("Spotify", {"Spotify": "paused", "Music": "paused"})
check("the player in front wins", media._active_player() == "Spotify")
scene("Safari", {"Spotify": "paused", "Music": "playing"})
check("else the one playing", media._active_player() == "Music")
scene("Safari", {"Spotify": "paused", "Music": "stopped"})
check("else the one with a song paused", media._active_player() == "Spotify")
scene("Safari", {"Spotify": "paused", "Music": "paused"}, order=["Music", "Spotify"])
check("two paused: the nearer window wins", media._active_player() == "Music")
scene("Safari", {})
check("nothing loaded: no guess", media._active_player() == "")
ctx.front_window, ctx.players, ctx._windows_quartz = real_fw, real_players, real_wq

# ------------------------------------------------------------ which app "play X" uses
print("\n=== which app 'play X' uses ===")
import jarvis.memory.prefs as prefs_mod
real_default = prefs_mod.get_default
real_installed, real_running = media.osa.app_installed, media.osa.app_running
media.osa.app_installed = lambda a: a in ("Spotify", "Music")
media.osa.app_running = lambda a: a in ("Spotify", "Music")
prefs_mod.get_default = lambda slot, d="": "Music"           # a stale saved default
ctx.front_window = lambda: ("Spotify", "")
ctx.players = lambda: [{"app": "Spotify", "state": "stopped"}]
check("Spotify on screen beats a saved Apple Music default", media._preferred_music_app() == "Spotify")
ctx.front_window = lambda: ("Safari", "")
check("with no player on screen the saved default applies", media._preferred_music_app() == "Music")
prefs_mod.get_default = lambda slot, d="": ""
ctx.players = lambda: []
check("no default, nothing in use: Spotify when installed", media._preferred_music_app() == "Spotify")

calls = []
media._play_on_music = lambda q, k: (calls.append("Music"), media.ToolResult(False, error="no subscription"))[1]
media._play_on_spotify = lambda q, k: (calls.append("Spotify"),
                                       media.ToolResult(True, content={"app": "Spotify", "title": q, "artist": "x"}))[1]
media._youtube_play = lambda q: (calls.append("YouTube"), media.ToolResult(True, content={}))[1]
r = media.play_music("Until I Found You", app="Music", kind="song")
check("Apple Music failing falls back to Spotify, not YouTube",
      r.ok and calls == ["Music", "Spotify"] and "instead" in r.content.get("note", ""), (calls, r.content))
calls.clear()
media._play_on_spotify = lambda q, k: (calls.append("Spotify"), media.ToolResult(False, error="not found"))[1]
r = media.play_music("Khushi", app="Spotify", kind="playlist")
check("a playlist nobody can find is not sent to YouTube",
      not r.ok and "YouTube" not in calls and "playlist" in r.error, (calls, r.error))
calls.clear()
r = media.play_music("some rare song", app="Spotify", kind="song")
check("a song neither app has ends on YouTube, with the reason",
      calls == ["Spotify", "Music", "YouTube"] and "why_youtube" in r.content, (calls, r.content))
prefs_mod.get_default = real_default
media.osa.app_installed, media.osa.app_running = real_installed, real_running
ctx.front_window, ctx.players = real_fw, real_players

# ------------------------------------------------------------ Spotify links
print("\n=== Spotify links ===")
import jarvis.tools.web as web
real_cascade = web._cascade
web._cascade = lambda kind, q, n, deadline=0: ([
    {"href": "https://www.youtube.com/watch?v=x", "title": "Flashing Lights - YouTube"},
    {"href": "https://open.spotify.com/album/25Ou2ydwOg3kOiqjIIhFZY", "title": "Flashing Lights - Single by Kanye West"},
    {"href": "https://open.spotify.com/track/5GZ4znceWfpTLrBBpr83DW", "title": "FlashingLights- song and lyrics byKanyeWest|Spotify"},
], "fake")
media._SPOTIFY_CACHE.clear()
check("a song resolves to its track URI",
      media._spotify_uri("Flashing Lights Kanye West", "song") == "spotify:track:5GZ4znceWfpTLrBBpr83DW")
media._SPOTIFY_CACHE.clear()
check("an album request takes the album",
      media._spotify_uri("Flashing Lights", "album") == "spotify:album:25Ou2ydwOg3kOiqjIIhFZY")
web._cascade = lambda kind, q, n, deadline=0: ([
    {"href": "https://open.spotify.com/album/2ZfHkwHuoAZrlz7RMj0PDz?highlight=spotify:track:0sf12qNH5qcw8qpgymFOqD",
     "title": "Blinding Lights - Single by The Weeknd"}], "fake")
media._SPOTIFY_CACHE.clear()
check("an album link that highlights a track gives the track",
      media._spotify_uri("Blinding Lights", "song") == "spotify:track:0sf12qNH5qcw8qpgymFOqD")
web._cascade = lambda kind, q, n, deadline=0: ([], "")
media._SPOTIFY_CACHE.clear()
check("nothing found: empty, not a search page", media._spotify_uri("zzqx", "song") == "")
web._cascade = real_cascade

# ------------------------------------------------------------ contacts
print("\n=== contacts ===")
check("'my dad' tries Papa", "papa" in comms._query_variants("my dad"))
check("'mummy' tries Mom", "mom" in comms._query_variants("mummy"))
people = [
    {"name": "Papa", "nickname": "", "org": "", "phones": ["98765 43210"], "emails": []},
    {"name": "Mark Lee", "nickname": "", "org": "", "phones": ["1"], "emails": []},
    {"name": "Rohan Sharma", "nickname": "Bhaiya", "org": "", "phones": ["2"], "emails": []},
]
comms._all_contacts = lambda: people
comms._related_name = lambda w: ""
check("'dad' finds the contact saved as Papa",
      [h["name"] for h in comms.resolve_contact("dad")] == ["Papa"], comms.resolve_contact("dad"))
check("'brother' finds the nickname Bhaiya",
      [h["name"] for h in comms.resolve_contact("brother")][:1] == ["Rohan Sharma"])
check("'ma' does not match Mark", all(h["name"] != "Mark Lee" for h in comms.resolve_contact("mom")))
comms._related_name = lambda w: "Rohan Sharma" if w == "sister" else ""
check("a relation on the user's own card is used",
      [h["name"] for h in comms.resolve_contact("sister")] == ["Rohan Sharma"])

comms._home_code = lambda: "91"
check("a local number gets the country code", comms._international("98765 43210") == "919876543210")
check("a leading 0 is a trunk prefix", comms._international("098765 43210") == "919876543210")
check("an international number is kept", comms._international("+1 (415) 555-0100") == "14155550100")
check("00 prefix is international", comms._international("0044 20 7946 0000") == "442079460000")

# ------------------------------------------------------------ playbooks
print("\n=== playbooks up front ===")
check("netflix brings the streaming playbook", (pb.for_request("play the mentalist on netflix") or [""])[0] == "streaming")
check("blender brings its playbook", (pb.for_request("make a cube in blender") or [""])[0] == "blender")
check("an ordinary word does not", pb.for_request("resolve this conflict and count the numbers") is None)
check("spotify music is not a streaming-video job", pb.for_request("play hozier on spotify") is None)

print(f"\n{sum(R)}/{len(R)} fast-action checks passed")
sys.exit(0 if all(R) else 1)
