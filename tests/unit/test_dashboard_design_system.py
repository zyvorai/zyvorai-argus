# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
"""The Liquid Glass design system: one shared token set, no outlined-box patterns,
and the hooks the JS and e2e tests rely on."""

from __future__ import annotations

import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"
CSS = TEMPLATES / "dashboard" / "css"


def _env() -> Environment:
    return Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True)


def _dashboard(desktop_mode: bool = False) -> str:
    return _env().get_template("dashboard.html.j2").render(refresh_interval_ms=5000, desktop_mode=desktop_mode)


def test_dashboard_and_login_share_one_token_set():
    tokens = (CSS / "_tokens.css.j2").read_text()
    token_names = set(re.findall(r"^\s*(--[a-z0-9-]+):", tokens, re.M))
    for required in ("--accent", "--surface", "--wallpaper", "--r-panel", "--r-control", "--m-pods", "--accent-fill"):
        assert required in token_names, required
    login = (TEMPLATES / "login.html.j2").read_text()
    assert "dashboard/css/_tokens.css.j2" in login
    assert not re.search(r"^\s*--bg:\s*#", login, re.M), "login must not carry its own copy of the tokens"
    assert "dashboard/css/_tokens.css.j2" in (TEMPLATES / "dashboard" / "styles.html.j2").read_text()


def test_light_and_dark_define_the_same_core_tokens():
    tokens = (CSS / "_tokens.css.j2").read_text()
    light = tokens[tokens.index('[data-theme="light"]'):]
    for name in ("--text", "--text-secondary", "--accent", "--surface", "--border", "--wallpaper", "--status-ok", "--status-error"):
        assert f"{name}:" in light, f"light theme is missing {name}"


def test_no_color_mix_for_older_webviews():
    # The Mac app's minimum macOS still ships a WebKit without color-mix().
    for path in list(CSS.glob("*.j2")) + [TEMPLATES / "login.html.j2"]:
        assert "color-mix(" not in path.read_text(), path.name


def test_old_outline_patterns_are_gone():
    css = "\n".join(p.read_text() for p in CSS.glob("*.j2"))
    assert "1px dashed" not in css, "empty states are quiet notes now, not dashed boxes"
    assert not re.search(r"\.run-btn\s*\{[^}]*border:\s*1px solid", css), "buttons are capsules, not outlined"
    assert not re.search(r"\.inp\s*\{[^}]*border:\s*1px solid var\(--border\)", css), "fields are fill-style"


def test_every_kpi_tile_has_an_icon_and_keeps_the_hooks():
    html = _dashboard()
    tiles = re.findall(r'<div class="stat-tile" data-metric="([a-z]+)"', html)
    assert tiles == ["pods", "replicas", "run", "rate", "cron", "knowledge"]
    assert html.count('class="kpi-ic"') == 6
    for hook in ('id="stat-pods"', 'id="stat-rate"', 'id="stat-knowledge"', 'class="stat-label"', 'class="stat-value"'):
        assert hook in html, hook
    for metric in ("pods", "replicas", "run", "rate", "cron", "knowledge"):
        assert f'.stat-tile[data-metric="{metric}"]' in (CSS / "_components.css.j2").read_text()


def test_header_has_title_search_and_theme_hooks():
    html = _dashboard()
    for hook in ('id="page-title"', 'id="page-sub"', 'id="topbar-palette"', 'class="search-btn"', 'id="theme-toggle"',
                 'id="knowledge-lamp"', 'id="target-switch"', 'class="capsule-group"'):
        assert hook in html, hook
    assert 'class="run-btn primary" id="topbar-palette"' not in html


def test_desktop_mode_still_hides_the_cluster_only_blocks():
    html = _dashboard(desktop_mode=True)
    assert 'data-metric="pods" style="display:none"' in html
    assert 'id="workloads"' in html  # kept in the DOM: the polling JS looks it up by id


def test_page_title_follows_the_active_panel():
    js = (TEMPLATES / "dashboard" / "scripts.html.j2").read_text()
    assert "function setPageTitle(id)" in js and "setPageTitle(id);" in js
    panels = set(re.findall(r'data-panel="([a-z]+)"', _dashboard()))
    declared = set(re.findall(r'\b([a-z]+): "', js[js.index("const PANEL_SUB"):js.index("function setPageTitle")]))
    assert panels <= declared, panels - declared


def test_reduced_transparency_falls_back_to_solid_surfaces():
    tokens = (CSS / "_tokens.css.j2").read_text()
    assert "prefers-reduced-transparency: reduce" in tokens
    assert "--glass-blur: none" in tokens


PANELS_A = ("pipeline", "visual", "quality", "probes", "journeys")


def _panel(name: str) -> str:
    return (TEMPLATES / "dashboard" / f"panel_{name}.html.j2").read_text()


def test_migrated_panels_have_no_inline_styles():
    for name in PANELS_A:
        assert 'style="' not in _panel(name), name


def test_migrated_panels_name_every_form_control():
    control = re.compile(r"<(input|select|textarea)\b([^>]*)>")
    for name in PANELS_A:
        html = _panel(name)
        for m in control.finditer(html):
            attrs = m.group(2)
            if re.search(r'type="(checkbox|radio|hidden)"', attrs):
                continue
            before = html[: m.start()]
            wrapped = before.rfind("<label") > before.rfind("</label>")
            named = re.search(r'\b(aria-label|aria-labelledby|title)="', attrs)
            idm = re.search(r'\bid="([^"]+)"', attrs)
            has_for = idm and re.search(r'<label[^>]*\bfor="%s"' % re.escape(idm.group(1)), html)
            assert wrapped or named or has_for, f"{name}: unlabeled control {attrs.strip()[:60]}"


def test_utility_classes_used_by_the_panels_exist():
    css = "\n".join(p.read_text() for p in CSS.glob("*.j2"))
    used = set()
    for name in PANELS_A:
        for cls in re.findall(r'class="([^"]*)"', _panel(name)):
            used.update(c for c in cls.split() if re.fullmatch(r"(ml-auto|pointer|span-all|fw-400|(mb|mt|gap|w|mw)-\d+)", c))
    assert used, "expected utility classes in the migrated panels"
    for cls in sorted(used):
        assert f".{cls} " in css or f".{cls}," in css or f".{cls}{{" in css, cls


def test_bare_source_and_device_selects_carry_a_visible_caption():
    html = "".join(_panel(n) for n in PANELS_A)
    assert html.count('class="select-field"') >= 9
    assert '<label class="select-field"><span>Source</span><select class="inp" id="run-source">' in html


PANELS_B = ("api", "requirements", "operations", "security")


def test_panels_b_keep_only_js_toggled_inline_styles():
    for name in PANELS_B:
        for style in re.findall(r'style="([^"]*)"', _panel(name)):
            assert style.replace(" ", "").startswith("display:none"), f"{name}: inline style {style!r}"


def test_panels_b_name_every_form_control():
    control = re.compile(r"<(input|select|textarea)\b([^>]*)>")
    for name in PANELS_B:
        html = _panel(name)
        for m in control.finditer(html):
            attrs = m.group(2)
            if re.search(r'type="(checkbox|radio|hidden)"', attrs):
                continue
            before = html[: m.start()]
            wrapped = before.rfind("<label") > before.rfind("</label>")
            named = re.search(r'\b(aria-label|aria-labelledby|title)="', attrs)
            idm = re.search(r'\bid="([^"]+)"', attrs)
            has_for = idm and re.search(r'<label[^>]*\bfor="%s"' % re.escape(idm.group(1)), html)
            assert wrapped or named or has_for, f"{name}: unlabeled control {attrs.strip()[:60]}"


def test_no_element_id_is_used_twice_across_panels():
    """Two panels once shared `au-url`/`au-insecure` and `cmp-a`/`cmp-b`; getElementById then
    returned the wrong panel's field (the auth test read the site-audit URL, and 'Compare runs'
    wrote its options into a text box)."""
    import collections
    from html.parser import HTMLParser

    class _Ids(HTMLParser):
        # A real parser: markup inside <script> strings is script text, not elements.
        def __init__(self):
            super().__init__()
            self.ids: list[str] = []

        def handle_starttag(self, tag, attrs):
            self.ids.extend(value for name, value in attrs if name == "id" and value)

    parser = _Ids()
    parser.feed(_dashboard())
    counts = collections.Counter(parser.ids)
    assert [i for i, c in counts.items() if c > 1] == []


def test_auth_and_visual_compare_read_their_own_fields():
    js = (TEMPLATES / "dashboard" / "scripts.html.j2").read_text()
    assert '$("auth-url")' in js and '$("auth-insecure")' in js
    assert '$("vcmp-a")' in js and '$("vcmp-b")' in js
    html = _dashboard()
    assert 'id="auth-url"' in html and 'id="vcmp-a"' in html
    assert 'id="cmp-a"' in html and 'aria-label="Baseline run"' in html  # the Compare-runs select keeps its id
