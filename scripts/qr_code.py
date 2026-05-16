"""
qr_code.py — generate a QR code with a centred TrueSight logo overlay.

Mirrors the style of the Agroverse QR generator (tokenomics/python_scripts/
agroverse_qr_code_generator/affiliate_link_qr_code.py) but with the TrueSight
icon. Used by build_cv_cache.py to produce a scannable badge that points back
to https://truesight.me/credentials/#<slug>.
"""

from __future__ import annotations

from pathlib import Path

import qrcode
from qrcode.constants import ERROR_CORRECT_H
from PIL import Image


DEFAULT_LOGO = Path(__file__).resolve().parent / 'truesight_icon.png'


def generate_qr_with_logo(
    url: str,
    out_path: Path,
    logo_path: Path = DEFAULT_LOGO,
    logo_ratio: float = 0.2,
    box_size: int = 10,
    border: int = 4,
) -> Path:
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_H,
        box_size=box_size,
        border=border,
    )
    qr.add_data(url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color='black', back_color='white').convert('RGBA')

    logo = Image.open(logo_path).convert('RGBA')
    qr_w, qr_h = qr_img.size
    max_logo_size = int(min(qr_w, qr_h) * logo_ratio)
    logo.thumbnail((max_logo_size, max_logo_size), Image.Resampling.LANCZOS)

    pos = ((qr_w - logo.width) // 2, (qr_h - logo.height) // 2)
    qr_img.paste(logo, pos, logo)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    qr_img.save(out_path)
    return out_path
