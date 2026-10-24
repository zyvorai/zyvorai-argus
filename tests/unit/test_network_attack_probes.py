# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0

"""Unit tests for network-attack / DAST probe modules."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agents.probes.csrf_probe import run_csrf_probe
from agents.probes.dast_scan import run_dast_scan
from agents.probes.injection_scan import run_injection_scan
from agents.probes.port_scan import DEFAULT_PORTS, run_port_scan
from agents.probes.tls_cipher_scan import _is_weak, run_tls_cipher_scan


def test_default_ports_capped():
    assert len(DEFAULT_PORTS) <= 64
    assert 443 in DEFAULT_PORTS


def test_port_scan_reports_open(monkeypatch):
    def fake_probe(host, port, timeout_s):
        return {"port": port, "open": port in (80, 443)}

    monkeypatch.setattr("agents.probes.port_scan._probe_port", fake_probe)
    data = run_port_scan("https://example.com", ports=[80, 443, 22])
    assert data["open_ports"] == [80, 443]
    assert data["scanned"] == 3


def test_weak_cipher_detection():
    assert _is_weak("RC4-SHA")
    assert _is_weak("TLS_RSA_WITH_NULL_SHA")
    assert not _is_weak("TLS_AES_256_GCM_SHA384")


def test_tls_cipher_scan_grades_failed_handshake(monkeypatch):
    monkeypatch.setattr(
        "agents.probes.tls_cipher_scan._try_connect",
        lambda *a, **k: {"ok": False, "error": "handshake fail"},
    )
    data = run_tls_cipher_scan("https://example.com")
    assert data["grade"] == "F"
    assert data["issues"]


def test_csrf_probe_flags_post_form_without_token():
    html = '<html><form method="POST" action="/save"><input name="email"></form></html>'
    resp = MagicMock()
    resp.text = html
    resp.url = "https://x.io/login"
    resp.headers = {"set-cookie": "sid=abc; Path=/"}  # no SameSite
    client = MagicMock()
    client.__enter__ = lambda s: s
    client.__exit__ = MagicMock(return_value=False)
    client.get.return_value = resp
    with patch("agents.probes.csrf_probe.client", return_value=client):
        data = run_csrf_probe("https://x.io/login")
    titles = [f["title"] for f in data["findings"]]
    assert any("CSRF token" in t for t in titles)
    assert any("SameSite" in t for t in titles)


def test_injection_scan_flags_sql_error():
    baseline = MagicMock()
    baseline.text = "ok"
    baseline.content = b"ok"
    bad = MagicMock()
    bad.text = "You have an error in your SQL syntax near '''"
    bad.content = bad.text.encode()

    client = MagicMock()
    client.__enter__ = lambda s: s
    client.__exit__ = MagicMock(return_value=False)

    def get(url, **kwargs):
        return bad if "OR" in url or "%27" in url or "'" in url else baseline

    client.get.side_effect = get
    with patch("agents.probes.injection_scan.client", return_value=client):
        data = run_injection_scan("https://x.io/item?id=1", max_requests=20)
    assert any(f["category"] == "sql-injection" for f in data["findings"])


def test_dast_scan_headers_module_only():
    resp = MagicMock()
    resp.text = "hi"
    resp.content = b"hi"
    resp.url = "https://x.io/"
    resp.headers = {}  # missing CSP etc.
    client = MagicMock()
    client.__enter__ = lambda s: s
    client.__exit__ = MagicMock(return_value=False)
    client.get.return_value = resp
    with patch("agents.probes.dast_scan.client", return_value=client):
        with patch("agents.probes.dast_scan.run_injection_scan", return_value={"findings": [], "requests": 0}):
            with patch("agents.probes.dast_scan.run_csrf_probe", return_value={"findings": [], "forms": []}):
                with patch("agents.probes.dast_scan._run_nuclei", return_value=[]):
                    data = run_dast_scan("https://x.io/", modules=["headers"])
    assert data["total"] >= 1
    assert any("content-security-policy" in f["title"] for f in data["findings"])
