"""JARVIS: an AI assistant that operates a Mac (files, apps, web, documents, media, Blender).

Entry point: `jarvis.cli.main` (the `jarvis` command). Start reading at `core/agent.py`,
the request loop; see README.md for the architecture and docs/ for the design notes.
"""




def code_stamp() -> str:
    """Changes whenever any JARVIS source file changes."""
    import os
    root = os.path.dirname(os.path.abspath(__file__))
    newest, count = 0.0, 0
    for d, _, files in os.walk(root):
        if "__pycache__" in d:
            continue
        for f in files:
            if f.endswith((".py", ".md")):
                count += 1
                newest = max(newest, os.path.getmtime(os.path.join(d, f)))
    return f"{newest:.0f}-{count}"
