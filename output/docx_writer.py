import os
import re
import tempfile
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

from pipeline._util import clean_text

# A YouTube description can run to pages of links and sponsor copy; the top is
# what describes the video. Teams recaps are written to be read in full.
_DESCRIPTION_MAX_CHARS = 1500


def write_docx(notes: dict, frames: list[dict], output_dir: str, safe_title: str,
               log_cb=None, source_info: dict | None = None) -> str:
    """Build the DOCX from structured notes and return the output file path."""
    doc = Document()
    _setup_styles(doc)

    _add_title(doc, clean_text(notes.get("title")).strip() or safe_title)
    if notes.get("degraded"):
        _add_degraded_notice(doc, notes["degraded"])
    _add_source_block(doc, source_info)

    frames_by_idx = {i + 1: f for i, f in enumerate(frames)}
    boxes = notes.get("screenshot_boxes") or {}
    video_url = (source_info or {}).get("url", "")

    chapters = notes.get("chapters")
    for chapter in chapters if isinstance(chapters, list) else []:
        if isinstance(chapter, dict):
            _add_chapter(doc, chapter, frames_by_idx, boxes, log_cb, video_url)

    os.makedirs(output_dir, exist_ok=True)
    out_path = _unused_path(output_dir, f"{safe_title}_notes")
    doc.save(out_path)
    return out_path


def check_output_dir(output_dir: str) -> str | None:
    """Return why a document can't be saved into output_dir, or None if it can.

    Run before the pipeline so an unwritable folder fails in seconds, not after
    the download and Claude call. A macOS privacy denial (e.g. no Desktop
    access) surfaces as FileExistsError from makedirs, so probe with a write.
    """
    try:
        os.makedirs(output_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=output_dir, prefix=".autonotes_probe_"):
            pass
    except OSError as e:
        return (f"Can't save to the output folder {output_dir} ({e.strerror or e}). "
                "Choose another folder, or allow AutoNotes access in System Settings "
                "→ Privacy & Security → Files and Folders.")
    return None


def _unused_path(output_dir: str, stem: str) -> str:
    """First free '<stem>.docx', '<stem> (2).docx', … — never replace a
    document the user may have edited or still have open in Word."""
    path = os.path.join(output_dir, f"{stem}.docx")
    n = 2
    while os.path.exists(path):
        path = os.path.join(output_dir, f"{stem} ({n}).docx")
        n += 1
    return path


def _setup_styles(doc: Document):
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)


def _add_title(doc: Document, title: str):
    p = doc.add_heading(title or "Notes", level=0)
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for run in p.runs:
        run.font.size = Pt(24)
    doc.add_paragraph()


def _add_degraded_notice(doc: Document, reason: str):
    """Say at the top that this is a transcript, not AI notes.

    Without it a fallback document is indistinguishable at a glance from a
    real one, and the reader has no idea the notes are missing.
    """
    p = doc.add_paragraph()
    run = p.add_run(f"{clean_text(reason)}, so this document contains the transcript "
                    "instead of AI notes. Run AutoNotes again with \u201cReuse the last "
                    "transcript\u201d ticked to retry without transcribing again.")
    run.italic = True
    run.font.color.rgb = RGBColor(0xA1, 0x4F, 0x00)


def _add_source_block(doc: Document, source_info: dict | None):
    """Recording metadata at the top of the document: type, URL/path, and
    the recording summary (YouTube description / Teams AI recap) if any."""
    if not source_info:
        return

    rec_type = clean_text(source_info.get("type")).strip()
    url = clean_text(source_info.get("url")).strip()
    summary = clean_text(source_info.get("summary")).strip()
    summary_label = clean_text(source_info.get("summary_label")).strip() or "Recording summary"
    if source_info.get("summary_is_description") and len(summary) > _DESCRIPTION_MAX_CHARS:
        cut = summary.rfind("\n", 0, _DESCRIPTION_MAX_CHARS)
        summary = summary[:cut if cut > _DESCRIPTION_MAX_CHARS // 2 else _DESCRIPTION_MAX_CHARS].rstrip() + "\n…"

    if rec_type:
        p = doc.add_paragraph()
        p.add_run("Source: ").bold = True
        p.add_run(rec_type)

    if url:
        p = doc.add_paragraph()
        p.add_run("Recording: ").bold = True
        if url.startswith(("http://", "https://")):
            _add_hyperlink(p, url, url)
        else:
            p.add_run(url)

    if summary:
        p = doc.add_paragraph()
        p.add_run(f"{summary_label}:").bold = True
        blank = False
        for line in summary.splitlines():
            line = line.rstrip()
            if not line:
                blank = True   # collapse runs of blank lines to one break
                continue
            sp = doc.add_paragraph()
            if blank:
                sp.paragraph_format.space_before = Pt(6)
                blank = False
            run = sp.add_run(line)
            run.font.size = Pt(10)
            run.font.color.rgb = RGBColor(0x44, 0x44, 0x44)

    doc.add_paragraph()


def _add_hyperlink(paragraph, url: str, text: str, size=None):
    """Insert a clickable external hyperlink (python-docx has no API for this)."""
    r_id = paragraph.part.relate_to(url, RT.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), r_id)
    run = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "1155CC")
    rpr.append(color)
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    rpr.append(underline)
    if size is not None:
        sz = OxmlElement("w:sz")
        sz.set(qn("w:val"), str(int(size.pt * 2)))   # half-points
        rpr.append(sz)
    run.append(rpr)
    text_el = OxmlElement("w:t")
    text_el.text = text
    run.append(text_el)
    link.append(run)
    paragraph._p.append(link)


def _add_chapter(doc: Document, chapter: dict, frames_by_idx: dict,
                 boxes: dict, log_cb=None, video_url: str = ""):
    heading = doc.add_heading(clean_text(chapter.get("title")).strip() or "Notes", level=1)
    for run in heading.runs:
        run.font.size = Pt(16)

    start = chapter.get("start_time")
    if isinstance(start, (int, float)) and not isinstance(start, bool) and start >= 0:
        _add_chapter_time(doc, start, chapter.get("end_time"), video_url)

    speakers = chapter.get("speakers")
    names = [clean_text(s).strip() for s in speakers if s is not None and clean_text(s).strip()] \
        if isinstance(speakers, list) else []
    if names:
        p = doc.add_paragraph()
        run = p.add_run(f"Speakers: {', '.join(names)}")
        run.italic = True
        run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
        run.font.size = Pt(10)

    points = chapter.get("key_points")
    for point in points if isinstance(points, list) else []:
        if isinstance(point, str):
            point = {"text": point}
        if not isinstance(point, dict):
            continue
        screenshot_idx = point.get("screenshot_idx")

        if screenshot_idx is not None and screenshot_idx in frames_by_idx:
            frame = frames_by_idx[screenshot_idx]
            _add_screenshot(doc, frame["path"], boxes.get(screenshot_idx), log_cb,
                            alt=_alt_text(point, frame))

        text = clean_text(point.get("text")).strip()
        if text:
            p = doc.add_paragraph(style="List Bullet")
            p.paragraph_format.left_indent = Inches(0.25)
            _add_formatted_run(p, text)

    doc.add_paragraph()


def _add_chapter_time(doc: Document, start: float, end, video_url: str):
    """'12:30 – 18:05' under the heading, linked into the video when possible."""
    label = _fmt_clock(start)
    if isinstance(end, (int, float)) and not isinstance(end, bool) and end > start:
        label += f" \u2013 {_fmt_clock(end)}"
    p = doc.add_paragraph()
    link = timestamp_url(video_url, start)
    if link:
        _add_hyperlink(p, link, label, size=Pt(10))
    else:
        run = p.add_run(label)
        run.font.size = Pt(10)
        run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)


def timestamp_url(url: str, seconds: float) -> str | None:
    """A link that opens the video at `seconds`, for hosts that support it."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if host in ("youtu.be",) or host == "youtube.com" or host.endswith(".youtube.com"):
        query = parse_qs(parsed.query)
        query["t"] = [f"{int(seconds)}s"]
        return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))
    return None


def _fmt_clock(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _alt_text(point: dict, frame: dict) -> str:
    """Describe a screenshot for screen readers: when it was taken and what it shows."""
    when = _fmt_clock(frame.get("timestamp") or 0)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", clean_text(point.get("text"))).strip()
    if len(text) > 200:
        text = text[:197].rstrip() + "…"
    return f"Screenshot at {when}" + (f": {text}" if text else "")


def _add_screenshot(doc: Document, image_path: str, box=None, log_cb=None, alt: str = ""):
    """Embed a screenshot, cropped to its content_box when one was provided.

    Fallback chain: cropped → full-frame Pillow re-encode (also covers JPEGs
    without a JFIF marker that python-docx rejects) → skip with a log line.
    """
    para = doc.add_paragraph()
    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = para.add_run()
    try:
        if box:
            shape = run.add_picture(_image_stream(image_path, box), width=Inches(5.5))
        else:
            shape = run.add_picture(image_path, width=Inches(5.5))
    except Exception as e:
        try:
            shape = run.add_picture(_image_stream(image_path, None), width=Inches(5.5))
        except Exception:
            if log_cb:
                log_cb(f"Skipped screenshot {image_path}: {type(e).__name__}: {e}")
            return
    if alt:
        # python-docx has no alt-text API; it lives on the drawing's docPr
        doc_pr = shape._inline.docPr
        doc_pr.set("descr", clean_text(alt))
        doc_pr.set("title", clean_text(alt.split(":", 1)[0]))


def _image_stream(image_path: str, box):
    """Return a JPEG BytesIO of the image, cropped to the fractional
    [x0, y0, x1, y1] box when given."""
    import io
    from PIL import Image

    img = Image.open(image_path)
    if box:
        w, h = img.size
        x0, y0, x1, y1 = box
        img = img.crop((round(x0 * w), round(y0 * h), round(x1 * w), round(y1 * h)))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=92)
    buf.seek(0)
    return buf


def _add_formatted_run(paragraph, text: str):
    """Parse **bold** markers and add appropriately styled runs."""
    parts = re.split(r"\*\*(.+?)\*\*", clean_text(text))
    for i, part in enumerate(parts):
        if not part:
            continue
        run = paragraph.add_run(part)
        run.font.size = Pt(11)
        if i % 2 == 1:
            run.bold = True
