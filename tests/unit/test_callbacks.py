# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
from __future__ import annotations

import json
from urllib.parse import urlsplit

import httpx
import pytest

from orchestrator import callbacks
from orchestrator.security.target_policy import TargetPolicyError


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ZYVOR_CALLBACKS_ENABLED", "true")
    monkeypatch.setenv("ZYVOR_CALLBACK_ALLOWLIST", "hooks.example.com")
    monkeypatch.setenv("ZYVOR_CALLBACK_SECRET", "s3cret")
    monkeypatch.setenv("ZYVOR_SKIP_TARGET_DNS_RESOLUTION", "true")
    monkeypatch.setenv("ZYVOR_ENV", "production")


class _Store:
    def __init__(self, url=None):
        self.url = url
        self.audits = []

    def get_job_callback(self, job_id):
        return self.url

    def audit(self, action, **kw):
        self.audits.append((action, kw))


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ZYVOR_CALLBACKS_ENABLED")
    with pytest.raises(ValueError, match="disabled"):
        callbacks.validate_callback_url("https://hooks.example.com/x")


def test_accepts_allowlisted_https_host():
    clean = urlsplit(callbacks.validate_callback_url("https://hooks.example.com/x"))
    assert (clean.scheme, clean.hostname, clean.path) == ("https", "hooks.example.com", "/x")


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.org/x",  # not on the allowlist
        "http://hooks.example.com/x",  # plain http in production
        "https://hooks.example.com:8443/x",  # port not allowed
        "https://user:pw@hooks.example.com/x",  # embedded credentials
        "https://metadata.google.internal/x",
        "ftp://hooks.example.com/x",
    ],
)
def test_rejects_unsafe_destinations(url):
    with pytest.raises(TargetPolicyError):
        callbacks.validate_callback_url(url)


def test_private_address_refused_even_if_allowlisted(monkeypatch):
    monkeypatch.setenv("ZYVOR_CALLBACK_ALLOWLIST", "127.0.0.1")
    monkeypatch.delenv("ZYVOR_SKIP_TARGET_DNS_RESOLUTION")
    with pytest.raises(TargetPolicyError):
        callbacks.validate_callback_url("https://127.0.0.1/x")


def test_slack_host_always_allowed_and_needs_no_secret(monkeypatch):
    monkeypatch.delenv("ZYVOR_CALLBACK_SECRET")
    assert callbacks.validate_callback_url("https://hooks.slack.com/services/T/B/X")


def test_non_slack_requires_secret(monkeypatch):
    monkeypatch.delenv("ZYVOR_CALLBACK_SECRET")
    with pytest.raises(ValueError, match="ZYVOR_CALLBACK_SECRET"):
        callbacks.validate_callback_url("https://hooks.example.com/x")


def test_payload_has_no_params_result_or_error(monkeypatch):
    monkeypatch.setenv("ZYVOR_PUBLIC_URL", "https://argus.example.com/")
    payload = callbacks.build_payload("j1", "smoke", "failed", 1.23456)
    assert payload == {
        "event": "job.finished",
        "job_id": "j1",
        "kind": "smoke",
        "status": "failed",
        "duration_s": 1.235,
        "link": "https://argus.example.com/api/v2/jobs/j1",
    }


def test_deliver_signs_the_exact_body(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        seen["headers"] = dict(request.headers)
        return httpx.Response(204)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    ok, detail = callbacks.deliver(
        "https://hooks.example.com/x", callbacks.build_payload("j1", "smoke", "succeeded"), client=client
    )
    assert ok and detail == "HTTP 204"
    ts = seen["headers"]["x-zyvor-timestamp"]
    assert seen["headers"]["x-zyvor-signature"] == callbacks.sign("s3cret", ts, seen["body"])
    assert json.loads(seen["body"])["job_id"] == "j1"


def test_deliver_slack_uses_text_payload_and_no_signature():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["headers"] = dict(request.headers)
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    ok, _ = callbacks.deliver(
        "https://hooks.slack.com/services/T/B/X", callbacks.build_payload("j1", "smoke", "failed"), client=client
    )
    assert ok
    assert "smoke" in seen["body"]["text"] and "failed" in seen["body"]["text"]
    assert "x-zyvor-signature" not in seen["headers"]


def test_deliver_retries_5xx_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503 if len(calls) < 3 else 200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    ok, _ = callbacks.deliver(
        "https://hooks.example.com/x", callbacks.build_payload("j", "k", "succeeded"), client=client, backoff_s=0
    )
    assert ok and len(calls) == 3


def test_deliver_does_not_retry_4xx():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(403)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    ok, detail = callbacks.deliver(
        "https://hooks.example.com/x", callbacks.build_payload("j", "k", "succeeded"), client=client, backoff_s=0
    )
    assert not ok and detail == "HTTP 403" and len(calls) == 1


def test_deliver_blocks_a_url_that_stops_passing_policy(monkeypatch):
    monkeypatch.setenv("ZYVOR_CALLBACK_ALLOWLIST", "other.example.com")  # changed after enqueue
    ok, detail = callbacks.deliver("https://hooks.example.com/x", {}, backoff_s=0)
    assert not ok and detail.startswith("blocked")


def test_dispatch_audits_host_but_never_the_url_or_query(monkeypatch):
    store = _Store("https://hooks.example.com/x?token=TOPSECRET")
    monkeypatch.setattr(callbacks, "deliver", lambda url, payload, **kw: (True, "HTTP 200"))
    monkeypatch.setattr(callbacks, "_executor", lambda: _Inline())
    callbacks.dispatch(store, "j1", "smoke", "succeeded", 1.0)
    (action, kw), = store.audits
    assert action == "job.callback" and kw["detail"] == {"host": "hooks.example.com", "result": "HTTP 200"}
    assert "TOPSECRET" not in json.dumps(store.audits)


def test_dispatch_ignores_non_terminal_disabled_and_urlless(monkeypatch):
    monkeypatch.setattr(callbacks, "_executor", lambda: pytest.fail("should not submit"))
    callbacks.dispatch(_Store("https://hooks.example.com/x"), "j", "k", "running")
    callbacks.dispatch(_Store(None), "j", "k", "succeeded")
    monkeypatch.delenv("ZYVOR_CALLBACKS_ENABLED")
    callbacks.dispatch(_Store("https://hooks.example.com/x"), "j", "k", "succeeded")


class _Inline:
    def submit(self, fn, *args):
        fn(*args)


def test_httpx_request_log_never_shows_the_callback_query(caplog):
    import logging

    caplog.set_level(logging.INFO, logger="httpx")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(204)))
    logging.getLogger("httpx").info('HTTP Request: %s %s "%s"', "POST", "https://hooks.example.com/x?token=TOPSECRET", "204")
    callbacks.deliver("https://hooks.example.com/x?token=TOPSECRET", {}, client=client, backoff_s=0)
    assert "TOPSECRET" not in caplog.text
    assert "hooks.example.com/x?[redacted]" in caplog.text
