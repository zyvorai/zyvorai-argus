#!/usr/bin/env python3
# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
"""Generate the Argus "Panoptes" brand SVGs.

Argus Panoptes was the hundred-eyed watchman: the mark is a ring of eight eyes
around a central pupil whose green dot is the product's health light. Run
`python3 brand/panoptes.py` to rewrite the SVGs in this directory, then
`brand/build-icons.sh` to rasterize the app/tray icons.

Files written here:
  argus-icon.svg           1024 app icon (squircle, full detail)
  argus-icon-small.svg     favicon / <=32px (thicker, no per-eye pupils)
  argus-mark.svg           the mark on a transparent background (UI use)
  argus-mark-template.svg  monochrome black, for the macOS menu-bar template icon
  share-card.svg           1200x630 social card
  ../templates/dashboard/_brand.html.j2
                           the same small icon as a Jinja macro (inline SVG for the
                           dashboard and login page) plus the favicon data URI
"""

from __future__ import annotations

import math
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
GREEN = "#30d158"
INK = "#0b0a1c"  # dark used for pupils on the icon background
VIOLET, CYAN = "#c58bff", "#2de2e6"


def almond(cx: float, cy: float, length: float, half_h: float, deg: float, fill: str) -> str:
    a = length / 2
    d = f"M{-a:.1f} 0Q0 {-2 * half_h:.1f} {a:.1f} 0Q0 {2 * half_h:.1f} {-a:.1f} 0Z"
    return f'<path transform="translate({cx:.1f} {cy:.1f}) rotate({deg:.1f})" d="{d}" fill="{fill}"/>'


def polar(r: float, deg: float) -> tuple[float, float]:
    return 512 + r * math.cos(math.radians(deg)), 512 + r * math.sin(math.radians(deg))


def defs(p: str, *, background: bool = True) -> str:
    bg = (
        f'<radialGradient id="{p}bg" cx="50%" cy="38%" r="75%">'
        f'<stop offset="0" stop-color="#2a2358"/><stop offset="1" stop-color="{INK}"/></radialGradient>'
        if background
        else ""
    )
    return (
        f"<defs>{bg}"
        f'<linearGradient id="{p}g" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{VIOLET}"/><stop offset="1" stop-color="{CYAN}"/></linearGradient></defs>'
    )


def eyes(p: str, *, length: float, half_h: float, radius: float, pupils: bool, pupil_fill: str) -> list[str]:
    out = []
    for i in range(8):
        ang = i * 45
        cx, cy = polar(radius, ang)
        out.append(almond(cx, cy, length, half_h, ang, f"url(#{p}g)"))
        if pupils:
            px, py = polar(radius + 8, ang)
            out.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="22" fill="{pupil_fill}"/>')
    return out


def icon(p: str = "ai") -> str:
    parts = [defs(p), f'<rect width="1024" height="1024" rx="229" fill="url(#{p}bg)"/>']
    parts += eyes(p, length=230, half_h=62, radius=292, pupils=True, pupil_fill=INK)
    parts += [
        f'<circle cx="512" cy="512" r="124" fill="url(#{p}g)"/>',
        f'<circle cx="512" cy="512" r="60" fill="{INK}"/>',
        f'<circle cx="512" cy="512" r="38" fill="{GREEN}"/>',
    ]
    return "\n".join(parts)


def icon_small(p: str = "as") -> str:
    parts = [defs(p), f'<rect width="1024" height="1024" rx="229" fill="url(#{p}bg)"/>']
    parts += eyes(p, length=270, half_h=84, radius=300, pupils=False, pupil_fill=INK)
    parts += [
        f'<circle cx="512" cy="512" r="150" fill="url(#{p}g)"/>',
        f'<circle cx="512" cy="512" r="82" fill="{INK}"/>',
        f'<circle cx="512" cy="512" r="56" fill="{GREEN}"/>',
    ]
    return "\n".join(parts)


def mark(p: str = "am") -> str:
    """Transparent background; pupils and the centre hole are knocked out with a mask."""
    cut = []
    for i in range(8):
        px, py = polar(300, i * 45)
        cut.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="22" fill="#000"/>')
    cut.append('<circle cx="512" cy="512" r="60" fill="#000"/>')
    mask = f'<mask id="{p}m"><rect width="1024" height="1024" fill="#fff"/>{"".join(cut)}</mask>'
    body = "\n".join(
        eyes(p, length=230, half_h=62, radius=292, pupils=False, pupil_fill=INK)
        + [f'<circle cx="512" cy="512" r="124" fill="url(#{p}g)"/>']
    )
    return (
        f'{defs(p, background=False)}{mask}<g mask="url(#{p}m)">{body}</g>'
        f'<circle cx="512" cy="512" r="38" fill="{GREEN}"/>'
    )


def template() -> str:
    """Pure black on transparent so macOS can tint it for light/dark menu bars."""
    cut = '<circle cx="512" cy="512" r="70" fill="#000"/>'
    body = []
    for i in range(8):
        cx, cy = polar(300, i * 45)
        body.append(almond(cx, cy, 270, 84, i * 45, "#000"))
    body.append('<circle cx="512" cy="512" r="150" fill="#000"/>')
    mask = f'<mask id="tm"><rect width="1024" height="1024" fill="#fff"/>{cut}</mask>'
    return f'{mask}<g mask="url(#tm)">{"".join(body)}</g><circle cx="512" cy="512" r="38" fill="#000"/>'


def svg(inner: str, size: int = 1024) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" width="{size}" height="{size}">\n'
        f"{inner}\n</svg>\n"
    )


def share_card() -> str:
    font = "-apple-system,Helvetica Neue,Helvetica,Arial,sans-serif"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">
<defs><radialGradient id="glow" cx="0.2" cy="0.35" r="0.7"><stop offset="0" stop-color="#7c6cff" stop-opacity="0.38"/><stop offset="1" stop-color="#000" stop-opacity="0"/></radialGradient></defs>
<rect width="1200" height="630" fill="#000"/><rect width="1200" height="630" fill="url(#glow)"/>
<g transform="translate(90,70) scale(0.3125)">
{icon("sc")}
</g>
<text x="90" y="470" font-family="{font}" font-size="84" font-weight="700" fill="#f5f5f7" letter-spacing="-2">Zyvor Argus</text>
<text x="90" y="530" font-family="{font}" font-size="36" fill="#a1a1a6">Autonomous QA for the real world</text>
<text x="90" y="582" font-family="{font}" font-size="26" fill="#a1a1a6">Requirements to tests to Mission Control. Now for Mac.</text>
</svg>
"""


def jinja_partial() -> str:
    """Inline-SVG macro + favicon, from the small icon, so templates never drift from brand/."""
    inline = icon_small("@@")
    macro_body = inline.replace("@@bg", "{{ uid }}-bg").replace("@@g", "{{ uid }}-g")
    favicon = svg(icon_small("fv"), 64).replace("\n", "")
    return (
        "{#- Generated by brand/panoptes.py. Do not edit by hand. -#}\n"
        "{% macro argus_icon(uid, size=32) -%}\n"
        f'<svg class="argus-icon" width="{{{{ size }}}}" height="{{{{ size }}}}" viewBox="0 0 1024 1024" aria-hidden="true" focusable="false">'
        f"{macro_body.replace(chr(10), '')}</svg>\n"
        "{%- endmacro %}\n"
        "{% macro zyvor_credit() -%}\n"
        '<a class="zyvor-mark" href="https://zyvor.dev/" target="_blank" rel="noopener noreferrer" aria-label="Zyvor home">'
        '<span class="mark-word">by Zyvor</span></a>\n'
        "{%- endmacro %}\n"
        "{% macro favicon_href() -%}\n"
        f"data:image/svg+xml,{quote(favicon, safe='')}\n"
        "{%- endmacro %}\n"
    )


def main() -> None:
    (HERE / "argus-icon.svg").write_text(svg(icon()))
    (HERE / "argus-icon-small.svg").write_text(svg(icon_small()))
    (HERE / "argus-mark.svg").write_text(svg(mark()))
    (HERE / "argus-mark-template.svg").write_text(svg(template()))
    (HERE / "share-card.svg").write_text(share_card())
    (HERE.parent / "templates" / "dashboard" / "_brand.html.j2").write_text(jinja_partial())


if __name__ == "__main__":
    main()
