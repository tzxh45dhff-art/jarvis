"""Document generation: Markdown, PDF, HTML. Used for reports and papers."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from ..safety import journal
from ..safety.policy import Risk, classify_path_write
from .registry import REGISTRY, ToolResult


def _p(path: str) -> Path:
    return Path(os.path.expanduser(path)).absolute()


@REGISTRY.tool(
    "write_document",
    "Write a plain document to disk (Markdown, simple PDF or HTML). For a "
    "designed document - cover, diagrams, charts, photos, pretty layout - use "
    "make_document instead.",
    {"type": "object", "properties": {
        "path": {"type": "string", "description": "Destination path, e.g. ~/Desktop/report.pdf"},
        "title": {"type": "string"},
        "markdown": {"type": "string", "description": "Document body in Markdown"},
        "format": {"type": "string", "enum": ["md", "pdf", "html"]}},
     "required": ["path", "markdown"]},
    risk=Risk.MEDIUM, category="write_new", parallel_safe=False,
    assess=lambda a: classify_path_write(a.get("path", "")), slow=True)
def write_document(path: str, markdown: str, title: str = "", format: str = ""):
    f = _p(path)
    fmt = (format or f.suffix.lstrip(".") or "md").lower()
    if fmt not in ("md", "pdf", "html"):
        fmt = "md"
    if f.suffix.lstrip(".").lower() != fmt:
        f = f.with_suffix("." + fmt)
    f.parent.mkdir(parents=True, exist_ok=True)
    existed = f.exists()
    journal.record("overwrite" if existed else "create", str(f),
                   session=REGISTRY.session_id)

    if fmt == "md":
        body = (f"# {title}\n\n" if title and not markdown.lstrip().startswith("#")
                else "") + markdown
        f.write_text(body)
    elif fmt == "html":
        f.write_text(_html(title, markdown))
    else:
        ok, err = _pdf(f, title, markdown)
        if not ok:
            return ToolResult(False, error=err)

    size = f.stat().st_size
    words = len(re.findall(r"\b\w+\b", markdown))
    return ToolResult(True,
                      content={"path": str(f), "bytes": size, "words": words,
                               "format": fmt},
                      display=f"wrote {f.name} ({fmt}, {words:,} words, {size:,} bytes)")


def _html(title: str, md: str) -> str:
    try:
        from markdown_it import MarkdownIt
        html = MarkdownIt("commonmark", {"html": False}).enable("table").render(md)
    except Exception:
        html = "<pre>" + md.replace("&", "&amp;").replace("<", "&lt;") + "</pre>"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{title or 'Document'}</title><style>
body{{max-width:46em;margin:3rem auto;padding:0 1.5rem;font:16px/1.65 -apple-system,
BlinkMacSystemFont,"Segoe UI",Georgia,serif;color:#1a1a1a}}
h1,h2,h3{{line-height:1.25;margin-top:2em}} h1{{font-size:2em;border-bottom:2px solid #eee;padding-bottom:.3em}}
table{{border-collapse:collapse;width:100%;margin:1.5em 0}}
th,td{{border:1px solid #ddd;padding:.5em .7em;text-align:left}}
th{{background:#f6f8fa}} code{{background:#f6f8fa;padding:.15em .35em;border-radius:3px;font-size:.9em}}
pre{{background:#f6f8fa;padding:1em;overflow-x:auto;border-radius:6px}}
blockquote{{border-left:4px solid #ddd;margin:0;padding-left:1em;color:#555}}
a{{color:#0366d6}} @media print{{body{{margin:0;max-width:none}}}}
</style></head><body>{html}</body></html>"""


def _pdf(f: Path, title: str, md: str) -> tuple[bool, str]:
    """Prefer pandoc if present; otherwise render with ReportLab."""
    pandoc = subprocess.run(["which", "pandoc"], capture_output=True, text=True).stdout.strip()
    if pandoc:
        tmp = f.with_suffix(".tmp.md")
        tmp.write_text((f"% {title}\n\n" if title else "") + md)
        r = subprocess.run([pandoc, str(tmp), "-o", str(f), "--pdf-engine=weasyprint"],
                           capture_output=True, text=True, timeout=180)
        tmp.unlink(missing_ok=True)
        if r.returncode == 0 and f.exists():
            return True, ""
    return _pdf_reportlab(f, title, md)


def _pdf_reportlab(f: Path, title: str, md: str) -> tuple[bool, str]:
    try:
        from reportlab.lib.enums import TA_JUSTIFY
        from reportlab.lib.pagesizes import LETTER
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.lib import colors
        from reportlab.platypus import (ListFlowable, ListItem, PageBreak,
                                        Paragraph, SimpleDocTemplate, Spacer, Table,
                                        TableStyle)
    except Exception as e:
        return False, f"PDF generation unavailable: {e}"

    ss = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=ss["BodyText"], fontName="Times-Roman",
                          fontSize=11, leading=15.5, alignment=TA_JUSTIFY,
                          spaceAfter=8)
    h1 = ParagraphStyle("h1", parent=ss["Heading1"], fontName="Helvetica-Bold",
                        fontSize=17, spaceBefore=18, spaceAfter=10)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontName="Helvetica-Bold",
                        fontSize=13.5, spaceBefore=14, spaceAfter=7)
    h3 = ParagraphStyle("h3", parent=ss["Heading3"], fontName="Helvetica-Bold",
                        fontSize=11.5, spaceBefore=11, spaceAfter=5)
    titlest = ParagraphStyle("t", parent=ss["Title"], fontName="Helvetica-Bold",
                             fontSize=21, leading=26, spaceAfter=20)
    mono = ParagraphStyle("code", parent=ss["Code"], fontName="Courier", fontSize=8.5,
                          leading=11, backColor=colors.HexColor("#f4f4f4"),
                          borderPadding=6, spaceAfter=10)

    def esc(s: str) -> str:
        s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<i>\1</i>", s)
        s = re.sub(r"`(.+?)`", r'<font face="Courier">\1</font>', s)
        s = re.sub(r"\[(.+?)\]\((.+?)\)", r'<link href="\2" color="blue">\1</link>', s)
        return s

    story = []
    if title:
        story += [Paragraph(esc(title), titlest)]
    lines = md.split("\n")
    i = 0
    bullets: list = []

    def flush_bullets():
        nonlocal bullets
        if bullets:
            story.append(ListFlowable(
                [ListItem(Paragraph(esc(b), body), leftIndent=14) for b in bullets],
                bulletType="bullet", start="•", leftIndent=16))
            story.append(Spacer(1, 4))
            bullets = []

    while i < len(lines):
        ln = lines[i]
        s = ln.strip()
        if s.startswith("```"):
            flush_bullets()
            i += 1
            buf = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i]); i += 1
            story.append(Paragraph("<br/>".join(
                x.replace("&", "&amp;").replace("<", "&lt;").replace(" ", "&nbsp;")
                for x in buf), mono))
        elif s.startswith("|") and i + 1 < len(lines) and set(
                lines[i + 1].strip().replace("|", "").replace(" ", "")) <= set("-:") \
                and lines[i + 1].strip():
            flush_bullets()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not set("".join(cells).replace(" ", "")) <= set("-:"):
                    rows.append([Paragraph(esc(c), ParagraphStyle(
                        "cell", parent=body, fontSize=9, leading=12, spaceAfter=0))
                        for c in cells])
                i += 1
            i -= 1
            if rows:
                t = Table(rows, hAlign="LEFT", colWidths=[
                    (6.5 * inch) / max(1, len(rows[0]))] * len(rows[0]))
                t.setStyle(TableStyle([
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#bbbbbb")),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef1f4")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5)]))
                story += [Spacer(1, 6), t, Spacer(1, 10)]
        elif s.startswith("### "):
            flush_bullets(); story.append(Paragraph(esc(s[4:]), h3))
        elif s.startswith("## "):
            flush_bullets(); story.append(Paragraph(esc(s[3:]), h2))
        elif s.startswith("# "):
            flush_bullets(); story.append(Paragraph(esc(s[2:]), h1))
        elif s in ("---", "***", "___"):
            flush_bullets(); story.append(PageBreak())
        elif re.match(r"^[-*+]\s+", s):
            bullets.append(re.sub(r"^[-*+]\s+", "", s))
        elif re.match(r"^\d+[.)]\s+", s):
            flush_bullets()
            story.append(Paragraph(esc(s), ParagraphStyle(
                "num", parent=body, leftIndent=16, spaceAfter=4)))
        elif not s:
            flush_bullets()
        else:
            flush_bullets(); story.append(Paragraph(esc(s), body))
        i += 1
    flush_bullets()

    doc = SimpleDocTemplate(str(f), pagesize=LETTER, title=title or f.stem,
                            leftMargin=0.95 * inch, rightMargin=0.95 * inch,
                            topMargin=1.0 * inch, bottomMargin=1.0 * inch)

    def page_num(canvas, _doc):
        canvas.saveState()
        canvas.setFont("Times-Roman", 9)
        canvas.setFillColor(colors.HexColor("#666666"))
        canvas.drawCentredString(LETTER[0] / 2, 0.55 * inch, str(canvas.getPageNumber()))
        canvas.restoreState()

    try:
        doc.build(story, onFirstPage=page_num, onLaterPages=page_num)
    except Exception as e:
        return False, f"PDF build failed: {e}"
    return True, ""


@REGISTRY.tool(
    "open_file",
    "Open a file or URL with the default macOS application (Preview, browser, etc). "
    "Use to show the user a finished document.",
    {"type": "object", "properties": {"path": {"type": "string"}},
     "required": ["path"]},
    risk=Risk.LOW, category="read", parallel_safe=False)
def open_file(path: str):
    target = path if path.startswith(("http://", "https://")) else str(_p(path))
    if not target.startswith("http") and not Path(target).exists():
        return ToolResult(False, error=f"Not found: {target}")
    subprocess.run(["open", target], capture_output=True, timeout=20)
    return ToolResult(True, content={"opened": target}, display=f"opened {Path(target).name}")
