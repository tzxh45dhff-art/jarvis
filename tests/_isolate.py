"""
Imported first by every test that runs the agent: memories, the learned
profile and preferences go to a throwaway folder, never the user's own.

test_integration once called get_store().clear() on the real store and then
wrote "favourite editor is Neovim" into it as if it were the user's fact.
"""
import os
import tempfile

_DIR = tempfile.mkdtemp(prefix="jarvis-test-data-")
os.environ["JARVIS_MEMORY_DB"] = os.path.join(_DIR, "memory.sqlite3")
os.environ["JARVIS_PROFILE_DB"] = os.path.join(_DIR, "profile.sqlite3")
os.environ["JARVIS_PREFS_DB"] = os.path.join(_DIR, "prefs.sqlite3")
os.environ["JARVIS_LEARNING"] = "0"          # no background learning from test prompts
os.environ["JARVIS_CLIPS_DB"] = os.path.join(_DIR, "clips.sqlite3")
os.environ["JARVIS_USAGE_LEDGER"] = os.path.join(_DIR, "usage.jsonl")   # test calls are not the user's spend
