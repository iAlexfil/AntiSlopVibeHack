"""Bounded, local PDF export of a sanitized report snapshot.

The HTTP layer owns snapshot authorization, storage, and concurrency. This
module reads no URLs or user files and accepts only embedded JPEG previews.
"""

from __future__ import annotations

import base64
import binascii
from io import BytesIO
from datetime import datetime, timezone
import math
from pathlib import Path
import threading
import unicodedata
from xml.sax.saxutils import escape

from PIL import Image as PillowImage, UnidentifiedImageError
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


MAX_IMAGES = 6
MAX_PREVIEW_BYTES = 1024 * 1024
MAX_PREVIEW_EDGE = 1600
MAX_TEXT_CHARS = 40_000
MAX_SECTIONS = 16
MAX_SECTION_ITEMS = 30
_JPEG_PREFIX = "data:image/jpeg;base64,"
_FONT = "AntiSlopPDF"
_BOLD = "AntiSlopPDF-Semibold"
_FONT_LOCK = threading.Lock()
_INK = colors.HexColor("#142732")
_MUTED = colors.HexColor("#5b6d77")
_LINE = colors.HexColor("#d8e2e7")
_ACCENT = colors.HexColor("#087a96")


def _register_fonts() -> None:
    with _FONT_LOCK:
        if _FONT in pdfmetrics.getRegisteredFontNames():
            return
        directory = Path(__file__).resolve().parent / "static" / "fonts"
        pdfmetrics.registerFont(TTFont(_FONT, str(directory / "manrope-pdf-regular.ttf")))
        pdfmetrics.registerFont(TTFont(_BOLD, str(directory / "manrope-pdf-semibold.ttf")))
        pdfmetrics.registerFontFamily(_FONT, normal=_FONT, bold=_BOLD, italic=_FONT, boldItalic=_BOLD)


def _text(value: object, limit: int = 1600) -> str:
    """Keep plain text only; never stringify containers or arbitrary objects."""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    value = str(value)
    truncated = len(value) > limit
    value = value[:limit]
    value = "".join(
        char for char in value
        if char in "\n\t" or not unicodedata.category(char).startswith("C")
    )
    return value.strip() + ("…" if truncated else "")


def _score(value: object) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 100 and math.isfinite(value):
        return f"{max(1, min(99, round(value)))}%"
    return None


def _preview(value: object) -> BytesIO | None:
    """Validate small JPEGs before decoding, then remove all embedded metadata."""
    if not isinstance(value, str) or not value.startswith(_JPEG_PREFIX):
        return None
    if len(value) > len(_JPEG_PREFIX) + 4 * ((MAX_PREVIEW_BYTES + 2) // 3):
        return None
    try:
        raw = base64.b64decode(value[len(_JPEG_PREFIX):], validate=True)
        if len(raw) > MAX_PREVIEW_BYTES:
            return None
        with PillowImage.open(BytesIO(raw)) as source:
            width, height = source.size
            if source.format != "JPEG" or not (0 < width <= MAX_PREVIEW_EDGE and 0 < height <= MAX_PREVIEW_EDGE):
                return None
            image = source.convert("RGB")
        try:
            image.info.clear()
            output = BytesIO()
            image.save(output, format="JPEG", quality=85, exif=b"")
        finally:
            image.close()
        output.seek(0)
        return output
    except (ValueError, OSError, binascii.Error, UnidentifiedImageError, PillowImage.DecompressionBombError):
        return None


def _list(value: object, limit: int) -> list:
    return value[:limit] if isinstance(value, list) else []


def render_pdf(snapshot: dict) -> bytes:
    """Return a complete PDF with embedded Cyrillic fonts, images, and pages.

    Supported snapshot version: 1. No HTML, JavaScript, network fetches, or
    document attachments are interpreted. Oversized fields are truncated;
    invalid previews become a text placeholder and do not break the report.
    """
    if not isinstance(snapshot, dict) or snapshot.get("version") != 1:
        raise ValueError("Неподдерживаемый формат отчёта")
    _register_fonts()
    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        leftMargin=46,
        rightMargin=46,
        topMargin=62,
        bottomMargin=54,
        title=_text(snapshot.get("title"), 180) or "Отчёт AntiSlop",
        author="AntiSlop",
        subject="Скрининговая проверка изображения на признаки генерации ИИ",
        pageCompression=1,
        allowSplitting=1,
    )
    styles = {
        "title": ParagraphStyle("title", fontName=_BOLD, fontSize=24, leading=31, textColor=_INK, spaceAfter=9),
        "score": ParagraphStyle("score", fontName=_BOLD, fontSize=48, leading=57, textColor=_ACCENT, spaceAfter=4),
        "label": ParagraphStyle("label", fontName=_BOLD, fontSize=11, leading=16, textColor=_ACCENT, spaceAfter=5),
        "headline": ParagraphStyle("headline", fontName=_BOLD, fontSize=15, leading=21, textColor=_INK, spaceAfter=8),
        "body": ParagraphStyle("body", fontName=_FONT, fontSize=10, leading=15, textColor=_INK, spaceAfter=8, splitLongWords=1),
        "muted": ParagraphStyle("muted", fontName=_FONT, fontSize=8, leading=12, textColor=_MUTED, spaceAfter=8, splitLongWords=1),
        "section": ParagraphStyle("section", fontName=_BOLD, fontSize=14, leading=20, textColor=_INK, spaceBefore=14, spaceAfter=9, keepWithNext=1),
        "item": ParagraphStyle("item", fontName=_BOLD, fontSize=10, leading=15, textColor=_INK, spaceAfter=4, splitLongWords=1),
        "caption": ParagraphStyle("caption", fontName=_FONT, fontSize=9, leading=13, textColor=_MUTED, alignment=TA_CENTER, spaceBefore=5, splitLongWords=1),
    }
    remaining = MAX_TEXT_CHARS

    def paragraph(value: object, style: str = "body", limit: int = 1600) -> Paragraph:
        nonlocal remaining
        text = _text(value, min(limit, max(0, remaining))) if remaining else ""
        remaining = max(0, remaining - len(text))
        return Paragraph(escape(text).replace("\n", "<br/>"), styles[style])

    is_series = snapshot.get("kind") == "series"
    story = [paragraph(snapshot.get("title") or ("Проверка серии" if is_series else "Проверка изображения"), "title", 180)]
    if snapshot.get("created_at"):
        created = _text(snapshot['created_at'], 80)
        try:
            created = datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%d.%m.%Y, %H:%M UTC")
        except ValueError:
            pass
        story.append(paragraph(f"Дата проверки: {created}", "muted", 110))
    if snapshot.get("label"):
        story.append(paragraph(snapshot["label"], "label", 180))
    score = _score(snapshot.get("score"))
    if score:
        story.extend([paragraph(score, "score"), paragraph("Вероятность генерации ИИ", "muted")])
    if snapshot.get("headline") and snapshot.get("headline") != snapshot.get("label"):
        story.append(paragraph(snapshot["headline"], "headline", 400))
    if snapshot.get("summary"):
        story.append(paragraph(snapshot["summary"], limit=2400))
    story.append(Spacer(1, 8))

    images = [item for item in _list(snapshot.get("images"), MAX_IMAGES) if isinstance(item, dict)]
    if images:
        columns = 2 if is_series and len(images) > 1 else 1
        cell_width = document.width / columns
        rows = []
        row = []
        for index, item in enumerate(images):
            preview = _preview(item.get("preview"))
            tile = []
            if preview is not None:
                image = Image(preview)
                ratio = min((cell_width - 20) / image.imageWidth, (150 if columns == 2 else 230) / image.imageHeight)
                image.drawWidth = image.imageWidth * ratio
                image.drawHeight = image.imageHeight * ratio
                image.hAlign = "CENTER"
                tile.append(image)
            else:
                tile.append(paragraph("Предпросмотр недоступен", "muted"))
            label = _text(item.get("label"), 160) or f"Снимок {index + 1}"
            image_score = _score(item.get("score"))
            if is_series and image_score:
                label += f" · ИИ: {image_score}"
            tile.append(paragraph(label, "caption", 200))
            row.append(tile)
            if len(row) == columns:
                rows.append(row)
                row = []
        if row:
            rows.append(row + [""] * (columns - len(row)))
        grid = Table(rows, colWidths=[cell_width] * columns, hAlign="LEFT")
        grid.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
        ]))
        story.extend([grid, Spacer(1, 8)])

    for section in _list(snapshot.get("sections"), MAX_SECTIONS):
        if not isinstance(section, dict) or remaining <= 0:
            continue
        items = [item for item in _list(section.get("items"), MAX_SECTION_ITEMS) if isinstance(item, dict)]
        if not items:
            continue
        story.append(paragraph(section.get("title") or "Детали проверки", "section", 180))
        for item in items:
            if remaining <= 0:
                break
            label = _text(item.get("label"), 180)
            value = _text(item.get("value"), 400)
            heading = ": ".join(text for text in (label, value) if text)
            detail = _text(item.get("detail"), 1800)
            if heading:
                # Keep each short heading with the start of its explanation.
                entry = paragraph(heading, "item", 600)
                entry.keepWithNext = bool(detail)
                story.append(entry)
            if detail:
                story.append(paragraph(detail, limit=1800))
            story.append(Spacer(1, 4))

    def draw_page(canvas, doc) -> None:
        canvas.saveState()
        width, height = A4
        canvas.setFont(_BOLD, 10)
        canvas.setFillColor(_INK)
        canvas.drawString(doc.leftMargin, height - 32, "ANTISLOP")
        canvas.setFont(_FONT, 8)
        canvas.setFillColor(_MUTED)
        canvas.drawRightString(width - doc.rightMargin, height - 32, "Проверка серии" if is_series else "Проверка изображения")
        canvas.setStrokeColor(_LINE)
        canvas.setLineWidth(0.6)
        canvas.line(doc.leftMargin, 41, width - doc.rightMargin, 41)
        canvas.drawString(doc.leftMargin, 27, "AntiSlop · Отчёт проверки")
        canvas.drawRightString(width - doc.rightMargin, 27, f"Страница {doc.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=draw_page, onLaterPages=draw_page)
    return output.getvalue()
