"""
Safety policy: classify every action, block the unrecoverable, make the rest undoable.

Design principle: JARVIS should not be able to destroy anything. Three layers.

1. HARD BLOCK   - patterns that are catastrophic and never justified.
2. CONFIRM      - reversible-but-consequential actions gated on explicit consent.
3. REVERSIBLE   - deletes go to Trash, overwrites snapshot the original first,
                  so "undo" is always available (see :mod:`jarvis.safety.journal`).
"""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

from ..config import CONFIG


class Risk(IntEnum):
    SAFE = 0        # read-only / trivially reversible
    LOW = 1         # creates new things
    MEDIUM = 2      # modifies existing things (snapshotted -> undoable)
    HIGH = 3        # deletes / system / outbound side effects -> confirm
    FORBIDDEN = 4   # never


@dataclass
class Verdict:
    risk: Risk
    reason: str = ""
    category: str = "generic"
    needs_confirm: bool = False
    detail: str = ""

    @property
    def allowed(self) -> bool:
        return self.risk < Risk.FORBIDDEN


# --- shell command analysis --------------------------------------------------
_FORBIDDEN = [re.compile(p, re.I) for p in CONFIG.safety.forbidden_patterns]

# Commands that are destructive or have outbound effects.
_DESTRUCTIVE_BINS = {
    "rm", "rmdir", "shred", "srm", "mkfs", "fdisk", "diskutil", "dd",
    "killall", "pkill", "shutdown", "reboot", "halt",
}
_SYSTEM_BINS = {
    "sudo", "su", "csrutil", "spctl", "nvram", "launchctl", "systemsetup",
    "scutil", "dscl", "visudo", "chown", "chmod", "chflags",
}
_INSTALL_BINS = {"brew", "pip", "pip3", "npm", "yarn", "pnpm", "bun", "gem",
                 "cargo", "go", "apt", "port", "uv"}
_NETWORK_WRITE = {"curl", "wget", "scp", "rsync", "ssh", "ftp", "nc", "git"}
_READ_ONLY_BINS = {
    "ls", "cat", "head", "tail", "grep", "rg", "find", "wc", "file", "stat",
    "du", "df", "ps", "top", "which", "whereis", "echo", "pwd", "date", "uname",
    "sw_vers", "sysctl", "env", "printenv", "id", "whoami", "hostname", "uptime",
    "man", "type", "basename", "dirname", "realpath", "sort", "uniq", "cut",
    "awk", "sed", "jq", "tree", "diff", "md5", "shasum", "open", "say", "sleep",
    "mdfind", "lsof", "netstat", "ifconfig", "defaults", "system_profiler",
}
# `git` subcommands that rewrite or publish history
_GIT_DANGEROUS = {"push", "reset", "clean", "rebase", "filter-branch", "gc", "prune"}


def _split_pipeline(cmd: str) -> list[list[str]]:
    """Split a shell string into token lists per simple command."""
    try:
        toks = shlex.split(cmd, posix=True)
    except ValueError:
        toks = cmd.split()
    segs, cur = [], []
    for t in toks:
        if t in ("|", "||", "&&", ";", "&"):
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        segs.append(cur)
    return segs


def classify_shell(cmd: str) -> Verdict:
    stripped = cmd.strip()
    if not stripped:
        return Verdict(Risk.SAFE, "empty")

    for pat in _FORBIDDEN:
        if pat.search(stripped):
            return Verdict(Risk.FORBIDDEN,
                           f"matches forbidden pattern: {pat.pattern}",
                           category="catastrophic")

    # Redirection into a device or a protected path
    if re.search(r">\s*/dev/(disk|rdisk)", stripped):
        return Verdict(Risk.FORBIDDEN, "raw device write", category="catastrophic")

    worst = Verdict(Risk.SAFE, "read-only")
    for seg in _split_pipeline(stripped):
        if not seg:
            continue
        bin_ = os.path.basename(seg[0])
        args = seg[1:]

        if bin_ in _SYSTEM_BINS:
            v = Verdict(Risk.HIGH, f"system-level command '{bin_}'",
                        category="system", needs_confirm=True)
        elif bin_ in _DESTRUCTIVE_BINS:
            v = Verdict(Risk.HIGH, f"destructive command '{bin_}'",
                        category="delete", needs_confirm=True)
        elif bin_ in _INSTALL_BINS and any(
                a in ("install", "add", "uninstall", "remove", "i") for a in args):
            v = Verdict(Risk.HIGH, f"package operation via '{bin_}'",
                        category="install", needs_confirm=True)
        elif bin_ == "git" and any(a in _GIT_DANGEROUS for a in args[:2]):
            v = Verdict(Risk.HIGH, f"git {args[0] if args else ''} rewrites or publishes",
                        category="network_write", needs_confirm=True)
        elif bin_ in _NETWORK_WRITE and bin_ != "git":
            # plain GET-style curl/wget is fine; uploads are not
            uploading = any(a in ("-d", "--data", "-F", "--upload-file", "-T",
                                  "--data-binary", "-X") for a in args)
            v = (Verdict(Risk.HIGH, f"outbound data via '{bin_}'",
                         category="network_write", needs_confirm=True)
                 if uploading else Verdict(Risk.LOW, f"network read via '{bin_}'",
                                           category="search"))
        elif bin_ == "osascript" and re.search(
                r"\b(quit|kill|close every|delete)\b", " ".join(args), re.I):
            # Quitting apps can lose unsaved work, which is why quit_apps asks
            # first - and AppleScript wrapped in `try` reports success even when
            # nothing closed. This path once did both: no prompt, exit 0, every
            # app still open.
            v = Verdict(Risk.HIGH, "quits or deletes through AppleScript - "
                                   "quit_apps does this and verifies it",
                        category="control", needs_confirm=True)
        elif bin_ in _READ_ONLY_BINS:
            v = Verdict(Risk.SAFE, "read-only", category="read")
        else:
            v = Verdict(Risk.LOW, f"unrecognised command '{bin_}'", category="generic")

        # output redirection = a write
        if re.search(r"(?<![0-9])>>?", " ".join(seg)) and v.risk < Risk.MEDIUM:
            v = Verdict(Risk.MEDIUM, "writes to a file via redirection",
                        category="overwrite")

        if v.risk > worst.risk:
            worst = v

    # Protected path touch. Only the arguments count: running a program that
    # lives in a protected folder (/Applications/Blender.app/.../Blender -b ...)
    # is not touching that folder - writing to it, or passing it to rm/mv, is.
    targets = " ".join(" ".join(seg[1:]) for seg in _split_pipeline(stripped) if seg)
    # Copying or moving INTO a protected folder writes there, whatever the
    # command's general risk; copying OUT of one only reads it.
    for seg in _split_pipeline(stripped):
        if seg and os.path.basename(seg[0]) in ("mv", "cp", "ditto", "rsync", "install", "ln"):
            dest = next((a for a in reversed(seg[1:]) if not a.startswith("-")), "")
            for p in CONFIG.safety.protected_paths:
                if re.match(rf"{re.escape(p)}(/|$)", os.path.expanduser(dest)):
                    return Verdict(Risk.FORBIDDEN, f"writes into protected path {p}",
                                   category="system")
    targets += " " + " ".join(m.group(1) for m in re.finditer(r">>?\s*(\S+)", stripped))
    for p in CONFIG.safety.protected_paths:
        if re.search(rf"(?<![\w/]){re.escape(p)}(/|\s|$)", targets) and worst.risk >= Risk.MEDIUM:
            return Verdict(Risk.FORBIDDEN, f"targets protected path {p}",
                           category="system")
    return worst


# --- filesystem path analysis ------------------------------------------------
def _resolve(p: str | os.PathLike) -> Path:
    return Path(os.path.expanduser(str(p))).resolve()


def classify_path_write(path: str, *, deleting: bool = False) -> Verdict:
    try:
        rp = _resolve(path)
    except Exception:
        return Verdict(Risk.HIGH, "unresolvable path", needs_confirm=True)

    s = str(rp)
    for prot in CONFIG.safety.protected_paths:
        if s == prot or s.startswith(prot + os.sep):
            return Verdict(Risk.FORBIDDEN, f"protected system path: {prot}",
                           category="system")
    # Its own source tree. A task about some other project must never reach in
    # here by accident - which is exactly how a stray "fix llm.py" once
    # overwrote the assistant's own system prompt and stopped it booting.
    from ..context import is_own_source
    if is_own_source(rp):
        return Verdict(Risk.HIGH, "this is JARVIS's own source code",
                       category="self_modify", needs_confirm=True,
                       detail="say so explicitly if you meant to change JARVIS")
    if s == str(Path.home()) or s == "/":
        return Verdict(Risk.FORBIDDEN, "refuses to operate on the root of home or /",
                       category="catastrophic")
    # Sensitive user data
    for sens in (".ssh", ".gnupg", ".aws", "Library/Keychains", ".config/gh"):
        if f"/{sens}" in s or s.endswith(f"/{sens}"):
            return Verdict(Risk.HIGH, f"touches credential store ({sens})",
                           category="system", needs_confirm=True)
    if deleting:
        return Verdict(Risk.HIGH, "deletion", category="delete", needs_confirm=True)
    if rp.exists():
        return Verdict(Risk.MEDIUM, "overwrites an existing file",
                       category="overwrite")
    return Verdict(Risk.LOW, "creates a new file", category="write_new")


def is_protected(path: str) -> bool:
    return classify_path_write(path).risk >= Risk.FORBIDDEN


def needs_confirmation(v: Verdict) -> bool:
    if v.risk >= Risk.FORBIDDEN:
        return False           # blocked outright, not confirmable
    if v.category in CONFIG.safety.auto_approve:
        return False
    return v.needs_confirm or v.risk >= Risk.HIGH


def describe(v: Verdict) -> str:
    return f"[{v.risk.name}/{v.category}] {v.reason}" + (f" — {v.detail}" if v.detail else "")
