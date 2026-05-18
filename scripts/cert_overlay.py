"""
cert_overlay.py — Phase 3b `pdf_overlay` strategy.

Open a partner-supplied PDF template, overlay recipient name + issued
date + QR code at coordinates declared on the truesight_me program
manifest, write the merged PDF.

Coordinate system is PDF native — points (1pt = 1/72 in), origin at
the bottom-left of the page. The overlay_fields config lives on
`truesight_me/programs/<slug>/manifest.json::certificate.overlay_fields`
(spec: agentic_ai_context/CREDENTIALING_PROGRAM_PAGES.md §17.13).

V1 supports single-page templates only.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class OverlayField:
    x_pt: float
    y_pt: float
    font: str = ""
    size_pt: float = 12.0
    anchor: str = "left"           # left | center | right
    color: str = "#000000"
    format: str = ""               # strftime pattern for date
    max_width_pt: float | None = None


def _hex_to_rgb01(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r = int(h[0:2], 16) / 255.0
    g = int(h[2:4], 16) / 255.0
    b = int(h[4:6], 16) / 255.0
    return r, g, b


def _build_overlay_canvas(
    page_size_pt: tuple[float, float],
    *,
    recipient_name: str,
    issued_at: datetime,
    qr_path: Path | None,
    fields: dict[str, dict[str, Any]],
    font_files: list[Path],
) -> bytes:
    """Render the overlay layer (text + QR) onto a transparent canvas the
    same size as the template page. Returns the PDF bytes for the layer."""
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    # Register every font file under a stable name (filename stem without
    # the bracketed Variable suffix). The manifest's `font` value must
    # match one of these names.
    name_to_font: dict[str, str] = {}
    for fp in font_files:
        # Stable, manifest-friendly name. Allow both
        # "EBGaramond-Italic-VariableFont_wght" (filename stem) and
        # "EBGaramond-Italic" (short alias) to resolve.
        stem = Path(fp).stem
        try:
            pdfmetrics.registerFont(TTFont(stem, str(fp)))
            name_to_font[stem] = stem
            short = stem.split("-VariableFont")[0]
            if short and short != stem:
                pdfmetrics.registerFont(TTFont(short, str(fp)))
                name_to_font[short] = short
        except Exception as e:
            logger.warning("failed to register font %s: %s", fp, e)

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=page_size_pt)

    def _draw_text(field_key: str, text: str) -> None:
        f = fields.get(field_key)
        if not f:
            return
        spec = OverlayField(
            x_pt=float(f.get("x_pt", 0)),
            y_pt=float(f.get("y_pt", 0)),
            font=f.get("font") or "Helvetica",
            size_pt=float(f.get("size_pt", 12)),
            anchor=f.get("anchor", "left"),
            color=f.get("color", "#000000"),
            max_width_pt=float(f["max_width_pt"]) if "max_width_pt" in f else None,
        )
        font_name = spec.font if spec.font in name_to_font else (
            list(name_to_font.values())[0] if name_to_font else "Helvetica"
        )
        size = spec.size_pt
        # Auto-shrink to fit if max_width_pt set.
        if spec.max_width_pt is not None and text:
            while size > 6 and pdfmetrics.stringWidth(text, font_name, size) > spec.max_width_pt:
                size -= 1
        c.setFont(font_name, size)
        c.setFillColorRGB(*_hex_to_rgb01(spec.color))
        if spec.anchor == "center":
            c.drawCentredString(spec.x_pt, spec.y_pt, text)
        elif spec.anchor == "right":
            c.drawRightString(spec.x_pt, spec.y_pt, text)
        else:
            c.drawString(spec.x_pt, spec.y_pt, text)

    # 1) Recipient name
    _draw_text("recipient_name", recipient_name or "")

    # 2) Date (strftime against issued_at; default ISO if no format spec)
    date_field = fields.get("date") or {}
    date_format = date_field.get("format") or "%Y-%m-%d"
    try:
        date_text = issued_at.strftime(date_format)
    except Exception:
        # macOS / Linux divergence on %-d etc. — fall back to a safe pattern.
        date_text = issued_at.strftime("%d %B %Y")
    _draw_text("date", date_text)

    # 3) QR (if a path was provided and the field is declared)
    qr_field = fields.get("qr")
    if qr_field and qr_path and Path(qr_path).is_file():
        size_pt = float(qr_field.get("size_pt", 64))
        x_pt = float(qr_field.get("x_pt", 0))
        y_pt = float(qr_field.get("y_pt", 0))
        # Anchor convention for QR matches text: 'left' (default) places the
        # bottom-left corner at (x_pt, y_pt); 'center' centres the square
        # around (x_pt, y_pt); 'right' anchors to the bottom-right corner.
        anchor = qr_field.get("anchor", "left")
        if anchor == "center":
            draw_x, draw_y = x_pt - size_pt / 2, y_pt - size_pt / 2
        elif anchor == "right":
            draw_x, draw_y = x_pt - size_pt, y_pt
        else:
            draw_x, draw_y = x_pt, y_pt
        # ReportLab insists on a real image reader; pass a path string.
        c.drawImage(
            str(qr_path),
            draw_x,
            draw_y,
            width=size_pt,
            height=size_pt,
            mask="auto",
            preserveAspectRatio=True,
        )

    c.showPage()
    c.save()
    return buf.getvalue()


def render_certificate_pdf_overlay(
    template_pdf: Path,
    out_path: Path,
    fields: dict[str, dict[str, Any]],
    *,
    recipient_name: str,
    issued_at: datetime,
    qr_path: Path | None,
    font_files: list[Path],
) -> Path:
    """Open `template_pdf`, overlay text + QR per `fields`, write the merged
    PDF to `out_path`. Returns the output path on success."""
    from pypdf import PdfReader, PdfWriter

    template_pdf = Path(template_pdf)
    out_path = Path(out_path)

    reader = PdfReader(str(template_pdf))
    if len(reader.pages) == 0:
        raise ValueError(f"template has no pages: {template_pdf}")
    page = reader.pages[0]
    box = page.mediabox
    page_size = (float(box.width), float(box.height))

    overlay_pdf_bytes = _build_overlay_canvas(
        page_size,
        recipient_name=recipient_name,
        issued_at=issued_at,
        qr_path=qr_path,
        fields=fields,
        font_files=[Path(p) for p in (font_files or [])],
    )
    overlay_reader = PdfReader(io.BytesIO(overlay_pdf_bytes))
    overlay_page = overlay_reader.pages[0]

    page.merge_page(overlay_page)

    writer = PdfWriter()
    writer.add_page(page)
    # Copy any subsequent template pages verbatim (no overlay).
    for extra in reader.pages[1:]:
        writer.add_page(extra)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as fh:
        writer.write(fh)
    return out_path
