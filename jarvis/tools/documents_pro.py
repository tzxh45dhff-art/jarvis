"""
Designed documents: reports, proposals, guides - with diagrams, charts and
photos, laid out like a professional publication.

The model writes Markdown; this module owns the design. It adds to plain
Markdown:

    ```mermaid            flowcharts, architecture / system diagrams, sequence,
    flowchart LR ...      gantt timelines, mind maps, ER diagrams, state machines
    ```                   (first line may be  %% caption: ...)

    ```chart              a chart drawn with make_chart
    {"kind": "bar", "title": "...", "categories": [...],
     "series": [{"name": "...", "values": [...]}]}
    ```

    ![Caption](web:solar farm aerial)   a free-to-use photo found on the web,
                                        credited under the picture
    ![Caption](~/Pictures/site.jpg)     a local image
    ![Caption](https://.../x.png)       an image from a link

    > [!NOTE] ...   > [!TIP]   > [!WARNING]   > [!IMPORTANT]   callout boxes

Rendering is done by the Google Chrome already on this Mac (headless), which
gives real typography, vector diagrams and proper page breaks. Then page
numbers are stamped on, and the first pages are rendered to a preview image so
the result can be looked at before it is called done.

Guardrails: a broken diagram stops the build with the exact error (no PDF with
a red error box in it); photos come only from openly licensed collections
(Openverse) and are always credited; nothing is overwritten.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path

from ..safety import journal
from ..safety.policy import Risk, Verdict, classify_path_write
from .fs import _p
from .registry import REGISTRY, ToolResult

log = logging.getLogger("jarvis.documents")

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
MERMAID_JS = "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"
PREVIEWS = Path(os.path.expanduser("~/.jarvis/data/previews"))
IMAGE_CACHE = Path(os.path.expanduser("~/.jarvis/data/images"))
MERMAID_LOCAL = Path(os.path.expanduser("~/.jarvis/cache/mermaid-11.4.1.min.js"))


def _mermaid_src() -> str:
    """A local copy of Mermaid, fetched once: builds then need no network for
    diagrams and skip a 2.6 MB download each time. Falls back to the CDN."""
    if MERMAID_LOCAL.exists() and MERMAID_LOCAL.stat().st_size > 500_000:
        return MERMAID_LOCAL.as_uri()
    try:
        import requests
        r = requests.get(MERMAID_JS, timeout=30)
        r.raise_for_status()
        if len(r.content) > 500_000 and b"mermaid" in r.content[:5000].lower() + r.content[-5000:].lower():
            MERMAID_LOCAL.parent.mkdir(parents=True, exist_ok=True)
            tmp = MERMAID_LOCAL.with_suffix(".part")
            tmp.write_bytes(r.content)
            tmp.replace(MERMAID_LOCAL)
            return MERMAID_LOCAL.as_uri()
    except Exception as e:                                # noqa: BLE001
        log.info("mermaid cache failed: %s", e)
    return MERMAID_JS

THEMES: dict[str, dict] = {
    "modern": dict(accent="#2563EB", accent2="#7C3AED", ink="#0F172A", muted="#64748B",
                   soft="#EFF4FF", rule="#E2E8F0", body_font="-apple-system, 'SF Pro Text', 'Helvetica Neue', Arial, sans-serif",
                   head_font="-apple-system, 'SF Pro Display', 'Helvetica Neue', Arial, sans-serif",
                   cover="linear-gradient(135deg, #1E3A8A 0%, #2563EB 55%, #7C3AED 100%)", cover_ink="#FFFFFF"),
    "classic": dict(accent="#1F3A5F", accent2="#9A6B2F", ink="#1B1B1B", muted="#5F6368",
                    soft="#F4F1EA", rule="#D9D4C7", body_font="'Iowan Old Style', 'Palatino', Georgia, serif",
                    head_font="'Iowan Old Style', 'Palatino', Georgia, serif",
                    cover="#F4F1EA", cover_ink="#1F3A5F"),
    "minimal": dict(accent="#111111", accent2="#6B7280", ink="#111111", muted="#6B7280",
                    soft="#F5F5F5", rule="#E5E5E5", body_font="'Helvetica Neue', Helvetica, Arial, sans-serif",
                    head_font="'Helvetica Neue', Helvetica, Arial, sans-serif",
                    cover="#FFFFFF", cover_ink="#111111"),
    "warm": dict(accent="#C2410C", accent2="#0F766E", ink="#292524", muted="#78716C",
                 soft="#FFF4EC", rule="#EADFD6", body_font="'Avenir Next', Avenir, 'Helvetica Neue', sans-serif",
                 head_font="'Avenir Next', Avenir, 'Helvetica Neue', sans-serif",
                 cover="linear-gradient(135deg, #7C2D12 0%, #C2410C 60%, #F59E0B 100%)", cover_ink="#FFFFFF"),
    "forest": dict(accent="#15803D", accent2="#0E7490", ink="#14251B", muted="#5B6B61",
                   soft="#EEF7F0", rule="#D7E6DB", body_font="'Avenir Next', Avenir, 'Helvetica Neue', sans-serif",
                   head_font="'Avenir Next', Avenir, 'Helvetica Neue', sans-serif",
                   cover="linear-gradient(135deg, #14532D 0%, #15803D 60%, #0E7490 100%)", cover_ink="#FFFFFF"),
}
DEFAULT_THEME = "modern"
PAGE = {"a4": ("210mm", "297mm"), "letter": ("8.5in", "11in")}


# ================================================================== web images

_OK_LICENSES = ("cc0", "pdm", "by", "by-sa")             # free for any use, with credit
_ALL_CC = _OK_LICENSES + ("by-nd", "by-nc", "by-nc-sa", "by-nc-nd")


def _openverse(query: str, n: int = 10, commercial: bool = True) -> list[dict]:
    import requests
    params = {"q": query, "page_size": max(5, min(n, 20)), "mature": "false"}
    params["license_type"] = "commercial" if commercial else "all-cc"
    r = requests.get("https://api.openverse.org/v1/images/", params=params, timeout=15,
                     headers={"User-Agent": "jarvis-local/1.0 (personal documents)"})
    r.raise_for_status()
    return r.json().get("results", [])


_IMG_FILLER = {"wikimedia", "commons", "openverse", "flickr", "openly", "licensed",
               "license", "licence", "free", "royalty", "cc0", "cc", "creative",
               "photo", "photos", "photograph", "photography", "image", "images",
               "picture", "pictures", "stock", "high", "resolution", "quality",
               "hd", "of", "a", "an", "the", "with", "and", "for", "in", "on"}


def _image_queries(query: str) -> list[str]:
    """The request, then shorter forms of it. Openverse needs every word to
    match, so long descriptive queries ('person paying with QR code smartphone
    India UPI street shop') found nothing in 9 of 21 real calls."""
    words = re.findall(r"[\w'-]+", query.lower())
    core = [w for w in words if w not in _IMG_FILLER] or words
    out = []
    for q in (" ".join(words), " ".join(core), " ".join(core[:3]), " ".join(core[:2])):
        if q and q not in out:
            out.append(q)
    return out


def _commons(query: str, n: int = 12) -> list[dict]:
    """Wikimedia Commons search, as Openverse-shaped rows. Used when Openverse
    is down or has nothing; every file there carries a free license."""
    import requests
    r = requests.get("https://commons.wikimedia.org/w/api.php", params={
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f"{query} filetype:bitmap", "gsrnamespace": 6, "gsrlimit": n,
        "prop": "imageinfo", "iiprop": "url|size|extmetadata", "iiurlwidth": 1800},
        timeout=10, headers={"User-Agent": "jarvis-local/1.0 (personal documents)"})
    r.raise_for_status()
    pages = ((r.json().get("query") or {}).get("pages") or {}).values()
    rows = []
    for pg in sorted(pages, key=lambda x: x.get("index", 0)):
        ii = (pg.get("imageinfo") or [{}])[0]
        md = ii.get("extmetadata") or {}

        def meta(k: str) -> str:
            return re.sub(r"<[^>]+>", "", str((md.get(k) or {}).get("value") or "")).strip()
        short = meta("LicenseShortName").lower()
        m = re.search(r"cc[ -]?(by(?:-sa|-nd|-nc|-nc-sa|-nc-nd)?|0)\s*([\d.]+)?", short)
        if "public domain" in short or short in ("pd", "cc0"):
            lic, ver = ("cc0" if "cc0" in short else "pdm"), ""
        elif m:
            lic, ver = ("cc0" if m.group(1) == "0" else m.group(1)), (m.group(2) or "")
        else:
            continue                                   # GFDL and friends: skip
        rows.append({
            "url": ii.get("thumburl") or ii.get("url") or "",
            "width": ii.get("thumbwidth") or ii.get("width") or 0,
            "height": ii.get("thumbheight") or ii.get("height") or 0,
            "title": re.sub(r"\.\w{3,4}$", "", (pg.get("title") or "").removeprefix("File:")),
            "creator": meta("Artist")[:80], "license": lic, "license_version": ver,
            "license_url": meta("LicenseUrl"),
            "foreign_landing_url": ii.get("descriptionurl") or "", "tags": []})
    return rows


def find_images_impl(query: str, count: int = 1, folder: Path | None = None,
                     errors: list | None = None) -> list[dict]:
    """Download up to `count` openly licensed images for `query`.
    Returns [{path, title, creator, license, license_url, source, credit, width, height}].
    Search-service failures are appended to `errors` when given, so the caller
    can tell "nothing matches" from "the service did not answer"."""
    import requests
    from PIL import Image
    folder = folder or IMAGE_CACHE
    folder.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    tried = set()
    errors = errors if errors is not None else []
    deadline = time.time() + 45
    down: set[str] = set()          # a service that timed out is not asked again
    attempts = []
    for q in _image_queries(query):
        attempts += [("openverse", q, True), ("openverse", q, False), ("commons", q, True)]
    for service, q, commercial in attempts:
        if len(results) >= count or time.time() > deadline:
            break
        if service in down:
            continue
        try:
            rows = (_openverse(q, 12, commercial) if service == "openverse"
                    else _commons(q, 12))
        except Exception as e:                            # noqa: BLE001
            log.info("%s failed: %s", service, e)
            errors.append(f"{service}: {str(e)[:120]}")
            if isinstance(e, (requests.Timeout, requests.ConnectionError)) or \
                    "429" in str(e):
                down.add(service)
            rows = []
        # Prefer pictures whose title and tags match the request, then big,
        # landscape ones; skip tiny thumbnails.
        words = set(re.findall(r"[a-z]{3,}", query.lower()))

        def score(r: dict) -> float:
            tags = [t.get("name", "") for t in (r.get("tags") or []) if isinstance(t, dict)]
            text = " ".join([r.get("title") or "", *tags]).lower()
            hits = sum(1 for w in words if w in text)
            w, h = r.get("width") or 0, r.get("height") or 1
            return hits * 2 + min(w, 2400) / 2400 * (1.0 if w >= h else 0.6)
        rows.sort(key=score, reverse=True)
        for r in rows:
            if len(results) >= count or time.time() > deadline:
                return results
            url = r.get("url") or ""
            if not url or url in tried or (r.get("width") or 0) < 640:
                continue
            tried.add(url)
            try:
                resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
                import io
                im = Image.open(io.BytesIO(resp.content))
                im = im.convert("RGB")
                im.thumbnail((1800, 1800))
                name = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")[:40] or "image"
                out = folder / f"{name}-{abs(hash(url)) % 10**8}.jpg"
                im.save(out, "JPEG", quality=86)
            except Exception:
                continue
            lic = (r.get("license") or "").lower()
            ver = r.get("license_version") or ""
            lic_label = "Public domain" if lic in ("cc0", "pdm") else f"CC {lic.upper()} {ver}".strip()
            creator = (r.get("creator") or "").strip() or "unknown"
            title = (r.get("title") or "").strip()[:80]
            src = r.get("foreign_landing_url") or url
            credit = f"Photo: {title + ' — ' if title else ''}{creator}, {lic_label}"
            results.append({"path": str(out), "title": title, "creator": creator,
                            "license": lic_label, "license_url": r.get("license_url") or "",
                            "source": src, "credit": credit,
                            "non_commercial": "nc" in lic, "width": im.width, "height": im.height})
        if results:
            break               # as before: a broader or non-commercial search only for nothing
    return results


@REGISTRY.tool(
    "find_images",
    "Find and download openly licensed photos from the web (Openverse: Flickr, "
    "museum collections; then Wikimedia Commons) for documents and slides. Give "
    "the subject in 1-4 plain words (e.g. 'QR code payment'). Returns local "
    "file paths plus the credit line that must be shown with each photo.",
    {"type": "object", "properties": {
        "query": {"type": "string", "description": "What the picture should show"},
        "count": {"type": "integer", "description": "How many, default 1, max 6"}},
     "required": ["query"]},
    risk=Risk.SAFE, category="search", parallel_safe=True, slow=True)
def find_images(query: str, count: int = 1):
    q = (query or "").strip()
    if not q:
        return ToolResult(False, error="what should the picture show?")
    errors: list[str] = []
    try:
        imgs = find_images_impl(q, max(1, min(int(count or 1), 6)), errors=errors)
    except Exception as e:                               # noqa: BLE001
        return ToolResult(False, error=f"image search failed: {e}"[:200])
    if not imgs:
        if errors:
            return ToolResult(False, error=(
                "the image services did not answer (" + "; ".join(errors[:2])
                + "). This is the network or the service, not your wording - try "
                  "again shortly, or go on without the picture."))
        return ToolResult(False, error=f"no openly licensed photo found for {q!r} "
                                       f"(also tried shorter forms); try other "
                                       f"words for the main subject")
    return ToolResult(True, content={"images": imgs, "verified": True,
                                     "note": "show each image's credit line with it"},
                      display=f"found {len(imgs)} image(s) for {q[:40]}")


# ================================================================== markdown → html

_FENCE = re.compile(r"^```(\w+)?[^\n]*\n(.*?)^```\s*$", re.S | re.M)
_IMG_LINE = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$", re.M)
_CALLOUT = re.compile(r"^> \[!(NOTE|TIP|WARNING|IMPORTANT|CAUTION)\]\s*(.*?)(?=\n(?!>)|\Z)", re.S | re.M)


class Build:
    def __init__(self, workdir: Path):
        self.dir = workdir
        self.assets = workdir / "assets"
        self.assets.mkdir(parents=True, exist_ok=True)
        self.warnings: list[str] = []
        self.diagrams: list[str] = []
        self.images: list[dict] = []
        self.charts = 0
        self.fig_no = 0

    def figure(self, inner: str, caption: str, credit: str = "", kind: str = "") -> str:
        self.fig_no += 1
        cap = ""
        if caption or credit:
            label = "<span class='fig-label'>Figure #</span> " if caption else ""
            cap = (f"<figcaption>{label}{html.escape(caption)}"
                   + (f"<span class='credit'>{html.escape(credit)}</span>" if credit else "")
                   + "</figcaption>")
        return f"\n\n<figure class='{kind}'>{inner}{cap}</figure>\n\n"

    def image(self, caption: str, src: str) -> str:
        src = src.strip().strip('"').strip("'")
        credit = ""
        local: Path | None = None
        try:
            if src.lower().startswith("web:"):
                q = src[4:].strip()
                found = find_images_impl(q, 1, self.assets)
                if not found:
                    self.warnings.append(f"no free photo found for {q!r}; left out")
                    return ""
                local, credit = Path(found[0]["path"]), found[0]["credit"]
                self.images.append(found[0])
            elif src.lower().startswith(("http://", "https://")):
                import requests
                r = requests.get(src, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
                r.raise_for_status()
                ext = os.path.splitext(urllib.parse.urlsplit(src).path)[1] or ".png"
                local = self.assets / f"web-{len(self.images)}{ext[:5]}"
                local.write_bytes(r.content)
                credit = "Source: " + urllib.parse.urlsplit(src).netloc
                self.images.append({"path": str(local), "source": src})
            else:
                p = _p(src)
                if not p.exists():
                    self.warnings.append(f"image not found: {src}")
                    return ""
                local = self.assets / f"local-{len(self.images)}{p.suffix}"
                shutil.copy2(p, local)
                self.images.append({"path": str(local)})
        except Exception as e:                            # noqa: BLE001
            self.warnings.append(f"image {src[:60]!r} could not be added: {e}"[:160])
            return ""
        rel = f"assets/{local.name}"
        return self.figure(f"<img src='{rel}' alt='{html.escape(caption)}'>", caption, credit, "photo")

    def chart(self, spec_text: str) -> str:
        try:
            spec = json.loads(spec_text)
        except Exception as e:
            self.warnings.append(f"a chart block is not valid JSON ({e}); left out")
            return ""
        from .media_edit import make_chart
        out = self.assets / f"chart-{self.charts + 1}.png"
        data = {"categories": spec.get("categories", []), "series": spec.get("series", [])}
        r = make_chart(kind=spec.get("kind", "column"), data=data, title=spec.get("title", ""),
                       x_label=spec.get("x_label", ""), y_label=spec.get("y_label", ""),
                       output=str(out))
        if not r.ok:
            self.warnings.append(f"chart could not be drawn: {r.error}")
            return ""
        self.charts += 1
        path = Path(r.content["path"])
        if path != out:
            shutil.move(str(path), str(out))
        return self.figure(f"<img src='assets/{out.name}' alt='chart'>",
                           spec.get("caption", spec.get("title", "")), "", "chart")

    def mermaid(self, src: str) -> str:
        src = src.strip("\n")
        cap = ""
        m = re.match(r"\s*%%\s*caption:\s*(.+)\n", src)
        if m:
            cap = m.group(1).strip()
            src = src[m.end():]
        self.diagrams.append(src)
        idx = len(self.diagrams) - 1
        first = src.strip().splitlines()[0].lower() if src.strip() else ""
        tall = bool(re.match(r"(flowchart|graph)\s+(tb|td|bt)\b", first))
        return self.figure(f"<pre class='mermaid' data-index='{idx}'>{html.escape(src)}</pre>",
                           cap, "", "diagram tall" if tall else "diagram")


def markdown_to_body(md_text: str, b: Build) -> tuple[str, list[tuple[int, str, str]]]:
    import markdown as mdlib

    # fenced special blocks first, so their contents are not treated as Markdown
    def fence(m):
        lang = (m.group(1) or "").lower()
        body = m.group(2)
        if lang == "mermaid":
            return b.mermaid(body)
        if lang == "chart":
            return b.chart(body)
        return m.group(0)
    text = _FENCE.sub(fence, md_text)

    text = _IMG_LINE.sub(lambda m: b.image(m.group(1), m.group(2)), text)

    def callout(m):
        kind = m.group(1).lower()
        inner = re.sub(r"^>\s?", "", m.group(2), flags=re.M).strip()
        title = {"note": "Note", "tip": "Tip", "warning": "Warning", "important": "Important",
                 "caution": "Caution"}[kind]
        body_html = mdlib.markdown(inner, extensions=["extra"])
        return (f"\n\n<div class='callout {kind}'><div class='callout-title'>{title}</div>"
                f"{body_html}</div>\n\n")
    text = _CALLOUT.sub(callout, text)

    md = mdlib.Markdown(extensions=["extra", "toc", "sane_lists", "codehilite"],
                        extension_configs={"codehilite": {"guess_lang": False, "css_class": "code"},
                                           "toc": {"permalink": False}})
    body = md.convert(text)
    heads: list[tuple[int, str, str]] = []

    def walk(tokens):
        for t in tokens:
            heads.append((t["level"], t["name"], t["id"]))
            walk(t.get("children", []))
    walk(getattr(md, "toc_tokens", []))
    n = iter(range(1, 10_000))
    body = re.sub(r"<span class='fig-label'>Figure #</span>",
                  lambda m: f"<span class='fig-label'>Figure {next(n)}</span>", body)
    return body, heads


# ================================================================== html page

def _css(th: dict, size: str) -> str:
    from pygments.formatters import HtmlFormatter
    w, h = PAGE.get(size, PAGE["a4"])
    code_css = HtmlFormatter(style="friendly").get_style_defs(".code")
    return f"""
@page {{ size: {w} {h}; margin: 22mm 20mm 24mm 20mm; }}
@page :first {{ margin: 0; }}
* {{ box-sizing: border-box; }}
html {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
body {{ font-family: {th['body_font']}; color: {th['ink']}; font-size: 10.6pt; line-height: 1.62;
        margin: 0; text-rendering: optimizeLegibility; font-kerning: normal; hyphens: auto; }}
.cover {{ width: {w}; height: {h}; background: {th['cover']}; color: {th['cover_ink']};
          display: flex; flex-direction: column; justify-content: flex-end; padding: 32mm 24mm 30mm;
          page-break-after: always; position: relative; overflow: hidden; }}
.cover::before {{ content: ""; position: absolute; right: -40mm; top: -40mm; width: 140mm; height: 140mm;
                  border-radius: 50%; background: rgba(255,255,255,0.08); }}
.cover::after {{ content: ""; position: absolute; right: 30mm; top: 60mm; width: 60mm; height: 60mm;
                 border-radius: 50%; background: rgba(255,255,255,0.06); }}
.cover .kicker {{ font-size: 10pt; letter-spacing: .22em; text-transform: uppercase; opacity: .85;
                  margin-bottom: 8mm; font-weight: 600; }}
.cover h1 {{ color: inherit; font-family: {th['head_font']}; font-size: 38pt; line-height: 1.08; margin: 0 0 6mm;
             font-weight: 750; letter-spacing: -.01em; max-width: 150mm; }}
.cover .subtitle {{ font-size: 15pt; opacity: .9; max-width: 140mm; line-height: 1.4; }}
.cover .meta {{ margin-top: 18mm; font-size: 10pt; opacity: .85; border-top: 1px solid currentColor;
                padding-top: 5mm; display: flex; gap: 12mm; }}
.toc {{ page-break-after: always; }}
.toc h2 {{ border: 0; margin-top: 0; }}
.toc ol {{ list-style: none; padding: 0; margin: 0; counter-reset: sec; }}
.toc li {{ padding: 2.2mm 0; border-bottom: 1px solid {th['rule']}; display: flex; gap: 4mm; }}
.toc li.l3 {{ padding-left: 9mm; font-size: 9.6pt; color: {th['muted']}; border-bottom: 0; padding-top: 1mm; padding-bottom: 1mm; }}
.toc a {{ color: inherit; text-decoration: none; }}
.toc .num {{ color: {th['accent']}; font-weight: 700; min-width: 7mm; }}
main {{ padding: 0; }}
h1, h2, h3, h4 {{ font-family: {th['head_font']}; color: {th['ink']}; line-height: 1.22; break-after: avoid;
                  page-break-after: avoid; }}
h1 {{ font-size: 24pt; margin: 0 0 6mm; font-weight: 750; letter-spacing: -.01em; }}
h2 {{ font-size: 16.5pt; margin: 11mm 0 4mm; padding-left: 4mm; border-left: 4px solid {th['accent']}; font-weight: 700; }}
h3 {{ font-size: 12.5pt; margin: 7mm 0 2.5mm; color: {th['accent']}; font-weight: 650; }}
h4 {{ font-size: 10.8pt; margin: 5mm 0 2mm; text-transform: uppercase; letter-spacing: .06em; color: {th['muted']}; }}
p {{ margin: 0 0 3.2mm; orphans: 3; widows: 3; text-align: justify; }}
a {{ color: {th['accent']}; text-decoration: none; }}
strong {{ color: {th['ink']}; }}
ul, ol {{ margin: 0 0 3.5mm; padding-left: 6mm; }}
li {{ margin: 0 0 1.4mm; }}
li::marker {{ color: {th['accent']}; }}
hr {{ border: 0; border-top: 1px solid {th['rule']}; margin: 8mm 0; }}
blockquote {{ margin: 5mm 0; padding: 3mm 6mm; border-left: 3px solid {th['accent2']}; color: {th['muted']};
              font-style: italic; font-size: 11.5pt; }}
table {{ width: 100%; border-collapse: collapse; margin: 4mm 0 6mm; font-size: 9.6pt; break-inside: avoid; }}
thead th {{ background: {th['accent']}; color: #fff; text-align: left; padding: 2.4mm 3mm; font-weight: 650; }}
tbody td {{ padding: 2.2mm 3mm; border-bottom: 1px solid {th['rule']}; vertical-align: top; }}
tbody tr:nth-child(even) td {{ background: {th['soft']}; }}
figure {{ margin: 6mm 0 7mm; text-align: center; break-inside: avoid; page-break-inside: avoid; }}
figure img {{ max-width: 100%; max-height: 120mm; border-radius: 3mm; }}
figure.photo img {{ box-shadow: 0 2mm 6mm rgba(0,0,0,.12); }}
figure.chart img {{ max-height: 95mm; }}
figure.diagram {{ background: #FFFFFF; border: 1px solid {th['rule']}; border-radius: 3mm; padding: 6mm 5mm 3mm; }}
figure.diagram pre.mermaid {{ background: transparent; margin: 0; padding: 0; border: 0; text-align: center; }}
figure.diagram svg {{ height: auto; max-height: 150mm; }}
figure.diagram.tall svg {{ max-height: 140mm; }}
figure.diagram .node rect, figure.diagram .node polygon, figure.diagram .node path,
figure.diagram .node circle, figure.diagram .node ellipse {{
  fill: {th['soft']} !important; stroke: {th['accent']} !important; stroke-width: 1.4px !important; }}
figure.diagram .node rect {{ rx: 7px; ry: 7px; }}
figure.diagram .cluster rect {{ fill: #FFFFFF !important; stroke: {th['rule']} !important;
  stroke-dasharray: 4 3; rx: 9px; ry: 9px; }}
figure.diagram .cluster-label, figure.diagram .cluster-label span {{ font-weight: 600; color: {th['muted']} !important; }}
figure.diagram .edgeLabel, figure.diagram .edgeLabel p {{ background: #FFFFFF !important; font-size: 13px; }}
figcaption {{ font-size: 8.8pt; color: {th['muted']}; margin-top: 2.5mm; line-height: 1.4; }}
figcaption .fig-label {{ font-weight: 700; color: {th['ink']}; margin-right: 1mm; }}
figcaption .credit {{ display: block; font-size: 7.6pt; opacity: .85; margin-top: .6mm; }}
.callout {{ border-radius: 2.5mm; padding: 3.5mm 5mm 1mm; margin: 5mm 0; break-inside: avoid;
            border: 1px solid; }}
.callout .callout-title {{ font-weight: 700; font-size: 9pt; letter-spacing: .08em; text-transform: uppercase; margin-bottom: 1.2mm; }}
.callout.note {{ background: #EFF6FF; border-color: #BFDBFE; }} .callout.note .callout-title {{ color: #1D4ED8; }}
.callout.tip {{ background: #ECFDF5; border-color: #A7F3D0; }} .callout.tip .callout-title {{ color: #047857; }}
.callout.warning, .callout.caution {{ background: #FFFBEB; border-color: #FDE68A; }}
.callout.warning .callout-title, .callout.caution .callout-title {{ color: #B45309; }}
.callout.important {{ background: #F5F3FF; border-color: #DDD6FE; }} .callout.important .callout-title {{ color: #6D28D9; }}
.callout p {{ text-align: left; }}
code {{ font-family: 'SF Mono', Menlo, monospace; font-size: 8.8pt; background: {th['soft']};
        padding: .3mm 1.2mm; border-radius: 1mm; }}
pre {{ background: #F8FAFC; border: 1px solid {th['rule']}; border-radius: 2.5mm; padding: 4mm;
       overflow: hidden; white-space: pre-wrap; font-size: 8.6pt; line-height: 1.5; break-inside: avoid; }}
pre code {{ background: none; padding: 0; }}
.code {{ background: #F8FAFC; border-radius: 2.5mm; }}
{code_css}
"""


def _mermaid_init(th: dict) -> str:
    vars_ = {"primaryColor": th["soft"], "primaryBorderColor": th["accent"],
             "primaryTextColor": th["ink"], "lineColor": th["muted"],
             "secondaryColor": "#FFFFFF", "tertiaryColor": "#FFFFFF",
             "fontFamily": "'Helvetica Neue', Helvetica, Arial, sans-serif", "fontSize": "16px",
             "clusterBkg": "#FFFFFF", "clusterBorder": th["rule"]}
    return ("mermaid.initialize({startOnLoad:true, securityLevel:'loose', theme:'base', "
            "flowchart:{curve:'basis', htmlLabels:true, padding:16, nodeSpacing:46, rankSpacing:52,"
            " subGraphTitleMargin:{top:6, bottom:10}}, "
            "gantt:{fontSize:13, sectionFontSize:13, barHeight:24, barGap:7, topPadding:56,"
            " leftPadding:96, gridLineStartPadding:40, axisFormat:'%d %b'}, "
            "sequence:{actorFontSize:14, messageFontSize:14, noteFontSize:13}, themeVariables:"
            + json.dumps(vars_) + "});")


def build_html(title: str, subtitle: str, author: str, theme: str, size: str, body: str,
               heads: list, cover: bool, toc: bool, kicker: str, mermaid_src: str = MERMAID_JS) -> str:
    th = THEMES.get(theme, THEMES[DEFAULT_THEME])
    parts = ["<!doctype html><html><head><meta charset='utf-8'>",
             f"<title>{html.escape(title)}</title><style>{_css(th, size)}</style>"]
    if "class='mermaid'" in body:
        parts.append(f"<script src='{mermaid_src}'></script><script>{_mermaid_init(th)}</script>")
    parts.append("</head><body>")
    if cover:
        date = time.strftime("%d %B %Y").lstrip("0")
        meta = "".join(f"<div>{html.escape(x)}</div>" for x in (author, date) if x)
        parts.append(f"<section class='cover'><div class='kicker'>{html.escape(kicker or 'Report')}</div>"
                     f"<h1>{html.escape(title)}</h1>"
                     + (f"<div class='subtitle'>{html.escape(subtitle)}</div>" if subtitle else "")
                     + f"<div class='meta'>{meta}</div></section>")
    h2s = [h for h in heads if h[0] in (2, 3)]
    if toc and sum(1 for h in h2s if h[0] == 2) >= 3:
        items, n = [], 0
        for lvl, name, hid in h2s:
            if lvl == 2:
                n += 1
                items.append(f"<li><span class='num'>{n:02d}</span><a href='#{hid}'>{html.escape(name)}</a></li>")
            else:
                items.append(f"<li class='l3'><a href='#{hid}'>{html.escape(name)}</a></li>")
        parts.append("<section class='toc'><h2>Contents</h2><ol>" + "".join(items) + "</ol></section>")
    parts.append(f"<main>{body}</main></body></html>")
    return "".join(parts)


# ================================================================== render

def _chrome(args: list[str], timeout: int = 90) -> subprocess.CompletedProcess:
    prof = tempfile.mkdtemp(prefix="jarvis-chrome-")
    try:
        return subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                               "--no-first-run", "--no-default-browser-check",
                               f"--user-data-dir={prof}", "--allow-file-access-from-files",
                               *args], capture_output=True, text=True, timeout=timeout)
    finally:
        shutil.rmtree(prof, ignore_errors=True)       # temporary profile created above


def check_diagrams(page: Path, n: int, widths: dict | None = None) -> list[tuple[int, str]]:
    """Render once and report every diagram Mermaid could not draw. Drawn
    widths (in CSS px, from each viewBox) go into `widths` when given."""
    if not n:
        return []
    r = _chrome(["--virtual-time-budget=15000", "--dump-dom", page.as_uri()])
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(r.stdout or "", "lxml")
    bad = []
    pres = soup.select("pre.mermaid")
    if not pres:
        return [(i, "the diagram library did not load (no internet?)") for i in range(n)]
    for pre in pres:
        idx = int(pre.get("data-index", -1))
        svg = pre.find("svg")
        txt = pre.get_text(" ", strip=True)
        if svg is None:
            bad.append((idx, "the diagram did not render (the diagram library may not have loaded)"))
        elif (svg.get("aria-roledescription") == "error"
              or "Syntax error" in txt or "Parse error" in txt):
            # (Every Mermaid drawing ships CSS for .error-icon, so that name
            # proves nothing; the error drawing itself is marked like this.)
            bad.append((idx, "Mermaid reported a syntax error"))
        elif widths is not None:
            try:
                widths[idx] = float((svg.get("viewbox") or svg.get("viewBox") or "").split()[2])
            except (IndexError, ValueError):
                pass
    return bad


# Room for a diagram on the page, in CSS px (165 mm). A left-to-right chart
# drawn much wider than this is shrunk until its labels cannot be read.
_DIAGRAM_ROOM = 624
_LR = re.compile(r"^(\s*(?:flowchart|graph)\s+)(LR|RL)\b", re.I)


def _stand_up(page: Path, b: "Build", widths: dict) -> int:
    """Turn sideways flowcharts that would print too small into top-to-bottom
    ones, so their labels stay readable. Returns how many were turned."""
    text = page.read_text()
    turned = 0
    for idx, w in widths.items():
        src = b.diagrams[idx]
        if w * 0.62 <= _DIAGRAM_ROOM or not _LR.match(src):
            continue
        new = _LR.sub(lambda m: m.group(1) + "TB", src, count=1)
        old_html = f"<figure class='diagram'><pre class='mermaid' data-index='{idx}'>{html.escape(src)}</pre>"
        new_html = f"<figure class='diagram tall'><pre class='mermaid' data-index='{idx}'>{html.escape(new)}</pre>"
        if old_html in text:
            text = text.replace(old_html, new_html, 1)
            b.diagrams[idx] = new
            turned += 1
    if turned:
        page.write_text(text)
    return turned


def _stamp_page_numbers(pdf: Path, title: str, skip_first: bool, th: dict) -> None:
    import io
    from pypdf import PdfReader, PdfWriter
    from reportlab.lib.colors import HexColor
    from reportlab.pdfgen import canvas
    rd = PdfReader(str(pdf))
    w = PdfWriter()
    total = len(rd.pages)
    for i, page in enumerate(rd.pages):
        if not (skip_first and i == 0):
            pw, ph = float(page.mediabox.width), float(page.mediabox.height)
            buf = io.BytesIO()
            c = canvas.Canvas(buf, pagesize=(pw, ph))
            c.setFont("Helvetica", 7.5)
            c.setFillColor(HexColor(th["muted"]))
            n = i + (0 if skip_first else 1)
            c.drawRightString(pw - 56, 30, f"{n} / {total - (1 if skip_first else 0)}")
            c.drawString(56, 30, title[:90])
            c.setStrokeColor(HexColor(th["rule"]))
            c.setLineWidth(0.5)
            c.line(56, 42, pw - 56, 42)
            c.save()
            buf.seek(0)
            page.merge_page(PdfReader(buf).pages[0])
        w.add_page(page)
    with open(pdf, "wb") as fh:
        w.write(fh)


def _preview(pdf: Path, pages: int = 4) -> str | None:
    """A contact sheet of the first pages, to look at before calling it done."""
    tool = shutil.which("pdftoppm") or "/opt/homebrew/bin/pdftoppm"
    if not os.path.exists(tool):
        return None
    PREVIEWS.mkdir(parents=True, exist_ok=True)
    # Page shots go in their own folder: globbing next to older previews once
    # swept the previous sheet in and then deleted it.
    shots_dir = Path(tempfile.mkdtemp(prefix="jarvis-shots-"))
    try:
        subprocess.run([tool, "-r", "55", "-f", "1", "-l", str(pages), "-png", str(pdf),
                        str(shots_dir / "p")], capture_output=True, timeout=60)
        shots = sorted(shots_dir.glob("p*.png"))
        if not shots:
            return None
        from PIL import Image
        ims = [Image.open(s).convert("RGB") for s in shots]
    finally:
        shutil.rmtree(shots_dir, ignore_errors=True)      # temporary folder created above
    gap = 14
    sheet = Image.new("RGB", (sum(i.width for i in ims) + gap * (len(ims) + 1),
                              max(i.height for i in ims) + 2 * gap), (205, 208, 214))
    x = gap
    for im in ims:
        sheet.paste(im, (x, gap))
        x += im.width + gap
    out = PREVIEWS / f"{pdf.stem.replace(' ', '_')}-preview.png"
    sheet.save(out)
    return str(out)


def _assess(args: dict) -> Verdict:
    v = classify_path_write(str(args.get("path", "")))
    if v.risk >= Risk.FORBIDDEN:
        return v
    return Verdict(Risk.LOW, "creates a document", category="write_new")


def _unique(p: Path) -> Path:
    if not p.exists():
        return p
    for i in range(2, 1000):
        c = p.with_name(f"{p.stem} ({i}){p.suffix}")
        if not c.exists():
            return c
    return p.with_name(f"{p.stem}-{int(time.time())}{p.suffix}")


@REGISTRY.tool(
    "make_document",
    "Create a beautifully designed document - PDF, Word (.docx: use this whenever "
    "the user asks for Word or an editable doc) or HTML, chosen by the path's "
    "extension: report, proposal, guide, research summary, plan, notes, letter. "
    "Write the body in Markdown (## for sections) "
    "and include, wherever they help: ```mermaid diagrams (flowchart, "
    "architecture with subgraphs, sequenceDiagram, gantt, mindmap, erDiagram, "
    "stateDiagram; first line '%% caption: ...'), ```chart blocks with JSON "
    "{kind, title, categories, series:[{name, values}]}, photos as "
    "![Caption](web:what to show) (free-licensed; the credit is added under the "
    "photo automatically, never write it yourself) or local paths, "
    "tables, and callouts '> [!NOTE] / [!TIP] / [!WARNING] / [!IMPORTANT]'. "
    "Adds a cover page, contents and page numbers. A broken diagram returns the "
    "error so you can fix it and call again. After it succeeds, look at the "
    "returned preview image before telling the user it is done. Themes: modern, "
    "classic, minimal, warm, forest.",
    {"type": "object", "properties": {
        "path": {"type": "string", "description": "e.g. ~/Desktop/Solar Proposal.pdf or ~/Desktop/Plan.docx"},
        "title": {"type": "string"},
        "subtitle": {"type": "string"},
        "author": {"type": "string"},
        "kicker": {"type": "string", "description": "Small label above the title, e.g. 'Proposal'"},
        "markdown": {"type": "string", "description": "The body. Do not repeat the title as '# ...'."},
        "theme": {"type": "string", "enum": list(THEMES)},
        "cover": {"type": "boolean", "description": "Cover page, default true"},
        "toc": {"type": "boolean", "description": "Contents page when there are 3+ sections, default true"},
        "page_size": {"type": "string", "enum": ["a4", "letter"]}},
     "required": ["path", "title", "markdown"]},
    risk=Risk.LOW, category="write_new", parallel_safe=False, slow=True, assess=_assess)
def make_document(path: str, title: str, markdown: str, subtitle: str = "", author: str = "",
                  kicker: str = "", theme: str = DEFAULT_THEME, cover: bool = True, toc: bool = True,
                  page_size: str = "a4"):
    if not os.path.exists(CHROME):
        return ToolResult(False, error="Google Chrome is needed to lay out documents; use "
                                       "write_document for a plain PDF")
    out = _p(path)
    fmt = out.suffix.lower().lstrip(".")
    if fmt in ("doc", "word"):
        out, fmt = out.with_suffix(".docx"), "docx"
    if fmt not in ("pdf", "html", "docx"):
        out = out.with_suffix(".pdf")
        fmt = "pdf"
    theme = theme if theme in THEMES else DEFAULT_THEME
    t0 = time.time()
    work = Path(tempfile.mkdtemp(prefix="jarvis-doc-"))
    try:
        b = Build(work)
        md_text = re.sub(r"^#\s+" + re.escape(title.strip()) + r"\s*$", "", markdown.strip(),
                         count=1, flags=re.M | re.I)
        body, heads = markdown_to_body(md_text, b)
        page = work / "index.html"
        mjs = _mermaid_src() if b.diagrams else MERMAID_JS
        page.write_text(build_html(title, subtitle, author, theme, page_size, body, heads,
                                   cover, toc, kicker, mjs))
        widths: dict = {}
        bad = check_diagrams(page, len(b.diagrams), widths)
        if not bad and _stand_up(page, b, widths):
            bad = check_diagrams(page, len(b.diagrams))
        if bad:
            details = []
            for idx, why in bad:
                src = b.diagrams[idx] if 0 <= idx < len(b.diagrams) else ""
                details.append({"diagram": idx + 1, "problem": why, "source": src[:600]})
            return ToolResult(False, content={"broken_diagrams": details},
                              error=(f"{len(bad)} diagram(s) could not be drawn - fix the Mermaid "
                                     f"syntax (quote labels with special characters, one statement "
                                     f"per line, valid arrows like --> ) and call again: "
                                     + json.dumps(details)[:1200]))
        out.parent.mkdir(parents=True, exist_ok=True)
        out = _unique(out)
        if fmt == "docx":
            from . import docx_writer
            dgm = docx_writer.render_diagrams(b, theme, page_size, build_html, _chrome)
            info = docx_writer.write_docx(out, title, subtitle, author, kicker, theme, page_size,
                                          body, heads, cover, toc, b.assets, dgm, THEMES[theme])
        elif fmt == "html":
            dest_assets = out.parent / (out.stem + "_files")
            shutil.copytree(b.assets, dest_assets, dirs_exist_ok=True)
            # A shared HTML file loads Mermaid from the CDN, not this Mac's cache.
            out.write_text(page.read_text().replace("assets/", dest_assets.name + "/")
                           .replace(mjs, MERMAID_JS))
        else:
            tmp_pdf = work / "out.pdf"
            r = _chrome(["--no-pdf-header-footer", "--virtual-time-budget=15000",
                         "--run-all-compositor-stages-before-draw",
                         f"--print-to-pdf={tmp_pdf}", page.as_uri()], timeout=120)
            if not tmp_pdf.exists() or tmp_pdf.stat().st_size < 1000:
                return ToolResult(False, error="Chrome could not produce the PDF: "
                                               + (r.stderr or "")[-300:])
            try:
                _stamp_page_numbers(tmp_pdf, title, cover, THEMES[theme])
            except Exception as e:                       # noqa: BLE001
                b.warnings.append(f"page numbers could not be added: {e}"[:120])
            shutil.copy2(tmp_pdf, out)
    finally:
        shutil.rmtree(work, ignore_errors=True)          # build folder created above
    journal.record("create", str(out), snapshot=False, session=REGISTRY.session_id)
    content: dict = {"path": str(out), "bytes": out.stat().st_size, "theme": theme,
                     "diagrams": len(b.diagrams), "charts": b.charts, "photos": len(b.images),
                     "seconds": round(time.time() - t0, 1), "verified": out.exists()}
    if fmt == "docx":
        from . import docx_writer
        pv = docx_writer.preview(out, PREVIEWS)
        if pv:
            content["preview"] = pv
            content["note"] = ("look at the first-page preview with look_at_image before "
                               "reporting; fix anything that looks wrong and build again. The "
                               "preview is Quick Look: it draws tables narrower and without "
                               "shading - in Word they span the page")
    if fmt == "pdf":
        try:
            from pypdf import PdfReader
            content["pages"] = len(PdfReader(str(out)).pages)
        except Exception:
            pass
        pv = _preview(out)
        if pv:
            content["preview"] = pv
            content["note"] = ("look at the preview with look_at_image before reporting; "
                               "fix anything that looks wrong and build again")
    credits = [i["credit"] for i in b.images if i.get("credit")]
    if credits:
        content["photo_credits"] = credits
    if any(i.get("non_commercial") for i in b.images):
        b.warnings.append("some photos are licensed for non-commercial use only")
    if b.warnings:
        content["warnings"] = b.warnings
    return ToolResult(True, content=content,
                      display=f"made {out.name} ({content.get('pages', '?')} pages, "
                              f"{len(b.diagrams)} diagrams, {b.charts} charts, {len(b.images)} photos)")
