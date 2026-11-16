"""Docker startup probe regressions; no Docker daemon, sockets or real sleeps."""

from __future__ import annotations

import http.client
import importlib
import io
from pathlib import Path
from types import SimpleNamespace
import urllib.error

import pytest


class Response(io.BytesIO):
    def __init__(self, body=b'{"status":"ok","active_runs":0,"draining":false}', status=200):
        super().__init__(body)
        self.status = status


@pytest.fixture
def probe(monkeypatch):
    deploy = Path(__file__).resolve().parents[1] / "infra" / "deploy"
    monkeypatch.syspath_prepend(str(deploy))
    module = importlib.import_module("smoke_image")
    clock = SimpleNamespace(now=0.0, sleeps=[])

    def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    return module, clock


@pytest.mark.parametrize("startup_error", [
    ConnectionResetError(104, "connection reset before application startup"),
    urllib.error.URLError(ConnectionRefusedError()),
    TimeoutError(),
    http.client.RemoteDisconnected(),
    http.client.IncompleteRead(b"", 1),
    urllib.error.HTTPError("http://local/health", 503, "warming up", {}, None),
])
def test_transient_startup_failure_then_ready_preserves_health_payload(probe, monkeypatch, startup_error):
    module, clock = probe
    calls = []

    def open_url(url, *, timeout):
        calls.append((url, timeout))
        if len(calls) == 1:
            raise startup_error
        return Response()

    monkeypatch.setattr(module.urllib.request, "urlopen", open_url)
    result = module.wait_for_health("http://127.0.0.1:1234/health")
    assert result == {"status": "ok", "active_runs": 0, "draining": False}
    assert len(calls) == 2
    assert clock.sleeps == [1]


def test_repeated_resets_stop_at_deadline_without_exposing_error_details(probe, monkeypatch):
    module, clock = probe
    timeouts = []

    def reset(url, *, timeout):
        timeouts.append(timeout)
        raise ConnectionResetError("private-response-detail")

    monkeypatch.setattr(module.urllib.request, "urlopen", reset)
    with pytest.raises(RuntimeError, match=r"within 2.5s \(ConnectionResetError\)") as failure:
        module.wait_for_health("http://127.0.0.1:1234/health", timeout_seconds=2.5)
    assert "private-response-detail" not in str(failure.value)
    assert timeouts == [2.5, 1.5, .5]
    assert clock.sleeps == [1, 1, .5]
    assert clock.now == 2.5


def test_http_200_without_ok_does_not_pass(probe, monkeypatch):
    module, clock = probe
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *args, **kwargs: Response(b'{"status":"starting"}'))
    with pytest.raises(RuntimeError, match="without status=ok"):
        module.wait_for_health("http://local/health", timeout_seconds=2)
    assert clock.now == 2


@pytest.mark.parametrize("code", [401, 403, 404])
def test_nontransient_http_failures_are_not_retried(probe, monkeypatch, code):
    module, clock = probe

    def forbidden(url, *, timeout):
        raise urllib.error.HTTPError(url, code, "private-response-detail", {}, None)

    monkeypatch.setattr(module.urllib.request, "urlopen", forbidden)
    with pytest.raises(RuntimeError, match=f"unexpected HTTP {code}"):
        module.wait_for_health("http://local/health")
    assert clock.sleeps == []


@pytest.mark.parametrize("body", [b"not-json", b"[]", b"null"])
def test_invalid_health_bodies_fail_immediately(probe, monkeypatch, body):
    module, clock = probe
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *args, **kwargs: Response(body))
    with pytest.raises(RuntimeError, match="JSON"):
        module.wait_for_health("http://local/health")
    assert clock.sleeps == []


def test_ok_body_requires_http_200(probe, monkeypatch):
    module, clock = probe
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *args, **kwargs: Response(status=201))
    with pytest.raises(RuntimeError, match="unexpected HTTP 201"):
        module.wait_for_health("http://local/health")
    assert clock.sleeps == []


def test_ready_response_after_deadline_does_not_pass(probe, monkeypatch):
    module, clock = probe

    def slow_ready(url, *, timeout):
        clock.now += 3
        return Response()

    monkeypatch.setattr(module.urllib.request, "urlopen", slow_ready)
    with pytest.raises(RuntimeError, match="ready response arrived after deadline"):
        module.wait_for_health("http://local/health", timeout_seconds=2)
    assert clock.sleeps == []
