# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
"""The Argus mark must be one design everywhere: the SVG sources, the generated
Jinja partial, the rendered pages, and the shipped PNGs."""

from __future__ import annotations

import importlib.util
import re
import struct
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[2]
BRAND = ROOT / "brand"


def _panoptes():
    spec = importlib.util.spec_from_file_location("panoptes", BRAND / "panoptes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    return struct.unpack(">II", data[16:24])


def test_committed_svgs_match_the_generator():
    pan = _panoptes()
    assert (BRAND / "argus-icon.svg").read_text() == pan.svg(pan.icon())
    assert (BRAND / "argus-icon-small.svg").read_text() == pan.svg(pan.icon_small())
    assert (BRAND / "argus-mark.svg").read_text() == pan.svg(pan.mark())
    assert (BRAND / "argus-mark-template.svg").read_text() == pan.svg(pan.template())
    assert (BRAND / "share-card.svg").read_text() == pan.share_card()
    partial = ROOT / "templates" / "dashboard" / "_brand.html.j2"
    assert partial.read_text() == pan.jinja_partial()


def test_site_favicon_is_the_small_icon():
    assert (ROOT / "site" / "favicon.svg").read_text() == (BRAND / "argus-icon-small.svg").read_text()


def test_template_mark_is_pure_black_so_macos_can_tint_it():
    svg = (BRAND / "argus-mark-template.svg").read_text()
    colours = set(re.findall(r'(?:fill|stroke)="(#[0-9a-fA-F]{3,6})"', svg))
    assert colours <= {"#000", "#fff"}, colours  # #fff only inside the knock-out mask


def test_shipped_png_sizes():
    icons = ROOT / "desktop" / "src-tauri" / "icons"
    expected = {
        "icon-source.png": 1024, "icon.png": 512, "128x128@2x.png": 256,
        "128x128.png": 128, "64x64.png": 64, "32x32.png": 32,
        "tray-template.png": 44, "tray-template@2x.png": 88,
    }
    for name, size in expected.items():
        assert _png_size(icons / name) == (size, size), name
    assert _png_size(ROOT / "site" / "apple-touch-icon.png") == (180, 180)
    assert _png_size(ROOT / "site" / "assets" / "argus-share-card.png") == (1200, 630)
    assert (icons / "icon.icns").read_bytes()[:4] == b"icns"


def _env() -> Environment:
    return Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=True)


def test_dashboard_uses_the_new_mark_and_no_longer_the_old_logos():
    html = _env().get_template("dashboard.html.j2").render(refresh_interval_ms=5000, desktop_mode=False)
    assert html.count('class="argus-icon"') == 2  # sidebar + mobile header
    assert "#ff5a15" not in html  # the red Zyvor Z is gone
    assert "mark-z" not in html
    assert "by Zyvor" in html
    assert '<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,' in html
    for gradient in ("rail-bg", "rail-g", "hdr-bg", "hdr-g"):
        assert html.count(f'id="{gradient}"') == 1, gradient  # unique per inline copy


def test_login_uses_the_new_mark():
    html = _env().get_template("login.html.j2").render()
    assert html.count('class="argus-icon"') == 2
    assert "#ff5a15" not in html and "mark-z" not in html
    assert '<link rel="icon"' in html


def test_dashboard_favicon_script_still_paints_a_status_light():
    html = _env().get_template("dashboard.html.j2").render(refresh_interval_ms=5000, desktop_mode=False)
    assert "function setFavicon(status)" in html
    assert "FAVICON_MARK_SRC" in html and "STATUS_DOT[status]" in html


def test_desktop_static_pages_use_the_new_mark_and_no_longer_the_orange_accent():
    public = ROOT / "desktop" / "public"
    assert (public / "argus-icon.svg").read_text() == (BRAND / "argus-icon-small.svg").read_text()
    index = (public / "index.html").read_text()
    assert 'src="argus-icon.svg"' in index
    for page in ("index.html", "settings.html"):
        assert "#ff8a3d" not in (public / page).read_text(), page


def test_tray_uses_the_template_icon_and_the_app_is_0_1_3():
    lib = (ROOT / "desktop" / "src-tauri" / "src" / "lib.rs").read_text()
    assert "tray-template@2x.png" in lib and "icon_as_template(true)" in lib
    import json

    conf = json.loads((ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text())
    assert conf["version"] == "0.1.3"
    assert f'version = "{conf["version"]}"' in (ROOT / "desktop" / "src-tauri" / "Cargo.toml").read_text()
    assert json.loads((ROOT / "desktop" / "package.json").read_text())["version"] == conf["version"]
    assert conf["app"]["windows"][0]["titleBarStyle"] == "Overlay"
