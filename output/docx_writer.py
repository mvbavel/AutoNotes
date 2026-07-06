import os
import re
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement


def write_docx(notes: dict, frames: list[dict], output_dir: str, safe_title: str,
               log_cb=None) -> str:
    """Build the DOCX from structured notes and return the output file path."""
    doc = Document()
    _setup_styles(doc)

    _add_title(doc, notes.get("title", safe_title))

    frames_by_idx = {i + 1: f["path"] for i, f in enumerate(frames)}

    for chapter in notes.get("chapters", []):
        _add_chapter(doc, chapter, frames_by_idx, log_cb)

    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, f"{safe_title}_notes.docx")
    doc.save(out_path)
    return out_path


def _setup_styles(doc: Document):
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)


def _add_title(doc: Document, title: str):
    p = doc.add_heading(title, level=0)
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.runs[0]
    run.font.size = Pt(24)
    doc.add_paragraph()


def _add_chapter(doc: Document, chapter: dict, frames_by_idx: dict, log_cb=None):
    heading = doc.add_heading(chapter.get("title", ""), level=1)
    heading.runs[0].font.size = Pt(16)

    speakers = chapter.get("speakers", [])
    if speakers:
        p = doc.add_paragraph()
        run = p.add_run(f"Speakers: {', '.join(speakers)}")
        run.italic = True
        run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
        run.font.size = Pt(10)

    for point in chapter.get("key_points", []):
        screenshot_idx = point.get("screenshot_idx")

        if screenshot_idx is not None and screenshot_idx in frames_by_idx:
            _add_screenshot(doc, frames_by_idx[screenshot_idx], log_cb)

        p = doc.add_paragraph(style="List Bullet")
        p.paragraph_format.left_indent = Inches(0.25)
        _add_formatted_run(p, point.get("text", ""))

    doc.add_paragraph()


def _add_screenshot(doc: Document, image_path: str, log_cb=None):
    para = doc.add_paragraph()
    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = para.add_run()
    try:
        run.add_picture(image_path, width=Inches(5.5))
    except Exception as e:
        # Safety net for image formats python-docx doesn't recognize
        # (e.g. JPEGs without a JFIF marker): re-encode via Pillow and retry
        try:
            import io
            from PIL import Image
            buf = io.BytesIO()
            Image.open(image_path).convert("RGB").save(buf, "JPEG", quality=92)
            buf.seek(0)
            run.add_picture(buf, width=Inches(5.5))
        except Exception:
            if log_cb:
                log_cb(f"Skipped screenshot {image_path}: {type(e).__name__}: {e}")


def _add_formatted_run(paragraph, text: str):
    """Parse **bold** markers and add appropriately styled runs."""
    parts = re.split(r"\*\*(.+?)\*\*", text)
    for i, part in enumerate(parts):
        if not part:
            continue
        run = paragraph.add_run(part)
        run.font.size = Pt(11)
        if i % 2 == 1:
            run.bold = True
