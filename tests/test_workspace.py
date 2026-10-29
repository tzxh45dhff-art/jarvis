"""
Where files resolve, and what the assistant is allowed to edit.

Regression cover for a real incident: asked to change llm.py with an editor open
on ~/Downloads/agentic ai, JARVIS resolved the name against its own process
working directory, found jarvis/core/llm.py, and edited itself - eventually
overwriting its own system prompt and failing to boot.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis import context
from jarvis.safety.policy import classify_path_write, needs_confirmation

R = []


def check(n, c, e=""):
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else f"\n        {e}"))
    R.append(bool(c))


print("=== editor window titles ===")
CASES = [
    ("agentic ai — llm.py", "llm.py", "agentic ai"),      # Antigravity order
    ("llm.py — agentic ai", "llm.py", "agentic ai"),      # VS Code order
    ("● main.py — myproj", "main.py", "myproj"),          # unsaved marker
    ("ViewController.swift — MyApp", "ViewController.swift", "MyApp"),
    ("fuck — browser.py", "browser.py", "fuck"),
]
for title, want_file, want_folder in CASES:
    got = context._parse_title(title)
    check(f"{title!r} -> {want_file}",
          got is not None and got[0] == want_file and got[1] == want_folder, got)

print("\n=== titles that are not documents ===")
for title in ("Activity Monitor", "WhatsApp", "Downloads", "", "Claude"):
    check(f"{title!r} is not a document", context._parse_title(title) is None,
          context._parse_title(title))

print("\n=== a title with a comma survives ===")
# window titles were once split on ', ' - any comma corrupted the parse
check("comma in the project name still parses",
      context._parse_title("notes, drafts — todo.md") == ("todo.md", "notes, drafts"),
      context._parse_title("notes, drafts — todo.md"))

print("\n=== path resolution ===")
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    (tmp / "target.txt").write_text("hello")
    os.environ["JARVIS_WORKSPACE"] = str(tmp)
    context.invalidate()
    check("workspace honours the override", context.workspace() == tmp,
          context.workspace())
    check("a relative name resolves inside the workspace",
          context.resolve("target.txt") == tmp / "target.txt",
          context.resolve("target.txt"))
    check("an absolute path is left alone",
          context.resolve("/etc/hosts") == Path("/etc/hosts"))
    check("~ still expands",
          context.resolve("~/Desktop") == Path.home() / "Desktop")
    check("a name that does not exist stays in the workspace",
          context.resolve("nope.txt") == tmp / "nope.txt",
          context.resolve("nope.txt"))
    check("an empty path means the workspace itself",
          context.resolve("") == tmp)
    os.environ.pop("JARVIS_WORKSPACE")
context.invalidate()

print("\n=== it will not quietly edit its own source ===")
own = context.INSTALL_ROOT / "jarvis" / "core" / "prompts.py"
check("its own tree is recognised", context.is_own_source(own), own)
check("and so is a file deeper inside it",
      context.is_own_source(context.INSTALL_ROOT / "jarvis" / "tools" / "fs.py"))
check("someone else's project is not",
      not context.is_own_source("/Users/x/Downloads/agentic ai/llm.py"))

v = classify_path_write(str(own))
check("writing to its own source is flagged", v.category == "self_modify", v)
check("and asks first", needs_confirmation(v), v)

with tempfile.TemporaryDirectory() as td:
    other = Path(td) / "llm.py"
    other.write_text("x")
    v2 = classify_path_write(str(other))
    check("an ordinary file is not gated", not needs_confirmation(v2), v2)

print("\n=== the daemon does not start inside the source tree ===")
src = (context.INSTALL_ROOT / "jarvis" / "daemon" / "client.py").read_text()
check("spawn uses home as the working directory",
      'cwd=os.path.expanduser("~")' in src)
check("but still puts the package on the import path",
      'env["PYTHONPATH"] = root' in src)

print("\n=== searching reaches outside the working directory ===")
from jarvis.tools import REGISTRY

check("find_files is registered", "find_files" in REGISTRY.names())
check("active_editor is registered", "active_editor" in REGISTRY.names())
r = REGISTRY.execute("find_files", {"pattern": "prompts.py"})
check("finds a file that is not under the process cwd",
      r.ok and any("prompts.py" in m for m in r.content["matches"]),
      r.content if r.ok else r.error)

print("\n=== dependency directories are excluded from text search ===")
import inspect

from jarvis.tools import fs
src = inspect.getsource(fs.search_text.__wrapped__
                        if hasattr(fs.search_text, "__wrapped__")
                        else fs.search_text)
check("the grep fallback excludes vendor dirs", "--exclude-dir=" in src, src[:200])
check("ripgrep excludes them too", "--glob" in src)

print(f"\n{sum(R)}/{len(R)} workspace checks passed")
sys.exit(0 if all(R) else 1)
