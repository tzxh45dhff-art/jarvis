"""
Downloads: fetch a file from a link, or wait for one a browser is saving.

JARVIS had no way to download anything - asked to, it improvised with curl in
the shell, or clicked a link and reported success without knowing whether a
file ever arrived. Both tools here end with a file on disk that has been
checked: the right size, not an HTML error page masquerading as a PDF, and
recorded in the undo journal like every other file JARVIS creates.

Programs and installers ask first, and keep macOS's quarantine flag so
Gatekeeper still inspects them before they can run.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
import urllib.parse
from pathlib import Path

from ..safety import journal
from ..safety.policy import Risk, Verdict
from .registry import REGISTRY, ToolResult

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
       "(KHTML, like Gecko) Version/18.0 Safari/605.1.15")
MAX_BYTES = 5 * 1024 ** 3
MAX_SECONDS = 15 * 60
_EXECUTABLE = {".dmg", ".pkg", ".mpkg", ".app", ".exe", ".msi", ".sh", ".command",
               ".scpt", ".jar", ".iso", ".bin", ".run", ".zsh", ".bash", ".py",
               ".tool", ".workflow"}
_PARTIAL = (".crdownload", ".download", ".part", ".partial", ".tmp", ".opdownload")
# Extensions whose real content is never an HTML page.
_BINARYISH = {".pdf", ".zip", ".dmg", ".pkg", ".png", ".jpg", ".jpeg", ".gif",
              ".mp3", ".mp4", ".mov", ".m4a", ".wav", ".docx", ".xlsx", ".pptx",
              ".epub", ".tar", ".gz", ".7z", ".iso", ".csv", ".json"}


def _safe_name(name: str) -> str:
    name = urllib.parse.unquote(name).strip().replace("/", "_").replace(":", "_")
    name = re.sub(r"[\x00-\x1f]", "", name)
    return name[:200] or "download"


def _name_for(url: str, headers) -> str:
    cd = headers.get("Content-Disposition", "") if headers else ""
    m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", cd, re.I) or \
        re.search(r'filename="?([^";]+)"?', cd, re.I)
    if m:
        return _safe_name(m.group(1).strip('"'))
    path = urllib.parse.urlparse(url).path
    base = os.path.basename(path.rstrip("/"))
    if base and "." in base:
        return _safe_name(base)
    ctype = (headers.get("Content-Type", "") if headers else "").split(";")[0]
    ext = {"application/pdf": ".pdf", "application/zip": ".zip",
           "image/png": ".png", "image/jpeg": ".jpg", "text/csv": ".csv",
           "application/json": ".json", "audio/mpeg": ".mp3",
           "video/mp4": ".mp4"}.get(ctype, "")
    return _safe_name((base or urllib.parse.urlparse(url).netloc or "download") + ext)


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suf = path.stem, path.suffix
    for i in range(2, 1000):
        p = path.with_name(f"{stem} ({i}){suf}")
        if not p.exists():
            return p
    return path.with_name(f"{stem} ({int(time.time())}){suf}")


def _ext_of(url: str, filename: str) -> str:
    name = filename or os.path.basename(urllib.parse.urlparse(url).path)
    return Path(name).suffix.lower()


def _assess_download(args: dict) -> Verdict:
    ext = _ext_of(args.get("url", ""), args.get("filename", ""))
    if ext in _EXECUTABLE:
        return Verdict(Risk.HIGH, f"downloads a program or installer ({ext})",
                       category="install", needs_confirm=True)
    return Verdict(Risk.LOW, "downloads a file", category="write_new")


def _quarantine(path: Path, url: str) -> None:
    """Mark like a browser would, so Gatekeeper checks it before first run."""
    try:
        value = f"0083;{int(time.time()):x};JARVIS;{url[:200]}"
        subprocess.run(["xattr", "-w", "com.apple.quarantine", value, str(path)],
                       capture_output=True, timeout=10)
    except Exception:
        pass


@REGISTRY.tool(
    "download_file",
    "Download a file from a URL to disk (default ~/Downloads) and verify it "
    "arrived complete - right size, and really the file rather than a login or "
    "error page. Use this for any 'download X' request where you have the link; "
    "for downloads that need a click on a website, click and then use "
    "wait_for_download.",
    {"type": "object", "properties": {
        "url": {"type": "string"},
        "folder": {"type": "string", "description": "Where to save, default ~/Downloads"},
        "filename": {"type": "string", "description": "Optional name to save as"}},
     "required": ["url"]},
    risk=Risk.LOW, category="write_new", parallel_safe=True, slow=True,
    assess=_assess_download)
def download_file(url: str, folder: str = "~/Downloads", filename: str = ""):
    import requests
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return ToolResult(False, error="give a full http(s) link to download")
    dest_dir = Path(os.path.expanduser(folder or "~/Downloads"))
    if not dest_dir.is_dir():
        return ToolResult(False, error=f"folder {dest_dir} does not exist")
    t0 = time.time()
    try:
        r = requests.get(url, headers={"User-Agent": _UA}, stream=True,
                         timeout=(15, 60), allow_redirects=True)
    except Exception as e:
        return ToolResult(False, error=f"could not reach {url}: {e}")
    if r.status_code >= 400:
        r.close()
        hint = (" - it probably needs a login; open it in the browser instead"
                if r.status_code in (401, 403) else "")
        return ToolResult(False, error=f"the server answered HTTP {r.status_code}{hint}")
    name = _safe_name(filename) if filename else _name_for(r.url or url, r.headers)
    target = _unique(dest_dir / name)
    ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
    expected = int(r.headers.get("Content-Length") or 0) or None
    if expected and expected > MAX_BYTES:
        r.close()
        return ToolResult(False, error=f"file is {expected / 1e9:.1f} GB - too large "
                                       f"to download automatically")
    part = target.with_name(target.name + ".part")
    h = hashlib.sha256()
    got = 0
    try:
        with open(part, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                if not chunk:
                    continue
                f.write(chunk)
                h.update(chunk)
                got += len(chunk)
                if got > MAX_BYTES or time.time() - t0 > MAX_SECONDS:
                    raise RuntimeError("download too large or too slow - stopped")
    except Exception as e:
        part.unlink(missing_ok=True)
        return ToolResult(False, error=f"download failed after {got:,} bytes: {e}")
    finally:
        r.close()
    if got == 0:
        part.unlink(missing_ok=True)
        return ToolResult(False, error="the server sent an empty file")
    if expected and got != expected:
        part.unlink(missing_ok=True)
        return ToolResult(False, error=f"incomplete: got {got:,} of {expected:,} bytes")
    ext = target.suffix.lower()
    if ctype == "text/html" and ext in _BINARYISH:
        head = part.read_bytes()[:400].lower()
        if b"<html" in head or b"<!doctype" in head:
            part.unlink(missing_ok=True)
            return ToolResult(False, error=(
                f"the link returned a web page, not the {ext} file - it probably "
                f"needs a login, a click on a download button, or it expired. "
                f"Open it in the browser and use wait_for_download."))
    part.rename(target)
    journal.record("create", str(target), snapshot=False,
                   session=REGISTRY.session_id)
    if ext in _EXECUTABLE:
        _quarantine(target, url)
    return ToolResult(True, content={
        "path": str(target), "bytes": got, "type": ctype or "unknown",
        "sha256": h.hexdigest(), "seconds": round(time.time() - t0, 1),
        "verified": True},
        display=f"downloaded {target.name} ({got / 1e6:.1f} MB)")


@REGISTRY.tool(
    "wait_for_download",
    "Wait for a download the browser is saving (after clicking a download "
    "button) to finish, then report the file. Ignores half-finished files.",
    {"type": "object", "properties": {
        "folder": {"type": "string", "description": "Default ~/Downloads"},
        "name_contains": {"type": "string",
                          "description": "Optional part of the expected file name"},
        "timeout": {"type": "integer", "description": "Seconds to wait, default 300"}},
     "required": []},
    risk=Risk.SAFE, category="read", slow=True)
def wait_for_download(folder: str = "~/Downloads", name_contains: str = "",
                      timeout: int = 300, _since: float | None = None):
    d = Path(os.path.expanduser(folder or "~/Downloads"))
    if not d.is_dir():
        return ToolResult(False, error=f"folder {d} does not exist")
    # A click usually happened moments before this call; look back a little.
    since = _since if _since is not None else time.time() - 90
    deadline = time.time() + max(5, min(int(timeout), 1800))
    want = name_contains.lower().strip()
    sizes: dict[Path, tuple[int, int]] = {}
    in_progress = []
    # If nothing has even started after this long, the click did not trigger a
    # download (often it opened a menu or a page instead). Saying so quickly
    # beats waiting out the whole timeout - measured: 2 minutes lost that way.
    start_by = time.time() + 20
    seen_any = False
    while time.time() < deadline:
        if not seen_any and time.time() > start_by:
            return ToolResult(False, error=(
                "no download started within 20 s of the click - it probably "
                "opened a menu or a page instead. Look at the page (browser_find) "
                "for the real download link, or use download_file with its URL."))
        in_progress = []
        done = []
        for p in d.iterdir():
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime < since or p.name.startswith("."):
                continue
            if want and want not in p.name.lower():
                continue
            seen_any = True
            if p.name.lower().endswith(_PARTIAL):
                in_progress.append(p.name)
                continue
            size = st.st_size if p.is_file() else sum(
                f.stat().st_size for f in p.rglob("*") if f.is_file())
            prev, stable = sizes.get(p, (-1, 0))
            sizes[p] = (size, stable + 1 if size == prev and size > 0 else 0)
            if sizes[p][1] >= 2:             # unchanged for two polls: finished
                done.append((st.st_mtime, p, size))
        if done and not in_progress:
            _, p, size = max(done)
            return ToolResult(True, content={"path": str(p), "bytes": size,
                                             "verified": True},
                              display=f"download finished: {p.name} ({size / 1e6:.1f} MB)")
        time.sleep(1.0)
    if in_progress:
        return ToolResult(False, content={"in_progress": in_progress},
                          error=f"still downloading after {timeout}s: "
                                f"{', '.join(in_progress[:3])}")
    return ToolResult(False, error=f"no new file appeared in {d} - the click may "
                                   f"not have started a download")
