"""Importing this package registers every tool."""
from .registry import REGISTRY, Tool, ToolResult  # noqa: F401


def load_all() -> "ToolRegistry":  # type: ignore[name-defined]
    from . import (ax, browser, downloads, comms, control, docs, documents_pro, files_plus, fs, guide,  # noqa: F401
                   macos, media_edit, online_plus, presentations,  # noqa: F401
                   find_anything, media, memory_tools, pim, playbook, profile_tools, quickfacts, research, screen,  # noqa: F401
                   shell, system_controls, system_plus, vision_ground, web)
    from . import toolsearch  # noqa: F401  (last: it defers tools registered above)
    return REGISTRY


load_all()
