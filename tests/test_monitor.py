"""Tests for the built-in monitoring API (token auth + endpoints)."""

import json
import threading
import urllib.request

import pytest

from bluray_to_mp4.monitor import JobRecord, MonitorState, start_server


def _get(url: str, token: str | None = None):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode())


@pytest.fixture
def server():
    state = MonitorState()
    srv, base = start_server(host="127.0.0.1", port=0, token="s3cret",
                             monitor_state=state)
    # start_server is idempotent (v0.1.3): a fresh monitor_state must never
    # be handed a reused global server, so reset the global after each
    # test to keep every case isolated on its own ephemeral port.
    yield srv, f"http://127.0.0.1:{srv.server_address[1]}", state
    srv.shutdown()
    srv.server_close()
    import bluray_to_mp4.monitor as mon
    mon.active_server = None


def test_health_open_status_token_gated(server):
    _, base, state = server
    state.begin("Test Disc", "bluray", series_mode=True, encoder="nvenc", jobs_total=4)

    # /health is always open and carries the busy flag
    code, payload = _get(base + "/health")
    assert code == 200 and payload["ok"] and payload["busy"]

    # /status requires the token
    with pytest.raises(Exception):
        _get(base + "/status")
    code, status = _get(base + "/status", token="s3cret")
    assert code == 200
    assert status["disc"] == "Test Disc"
    assert status["series_mode"] is True and status["jobs_total"] == 4

    # ?token= query works too (handy from a browser or curl)
    code, status = _get(base + "/status?token=s3cret")
    assert code == 200 and "progress" in status


def test_progress_and_history(server):
    _, base, state = server
    state.begin("Movie", "bluray", series_mode=False, encoder="x265", jobs_total=1)
    state.update(stage="encoding", progress=0.5, eta_seconds=600)
    state.job_done(JobRecord(name="Movie (2019).mp4", kind="movie",
                             duration_seconds=7200, encoder="x265", srt=True))
    state.finish()

    code, status = _get(base + "/status", token="s3cret")
    assert code == 200
    assert status["stage"] == "idle" and not status["busy"]
    assert status["progress"] == 0.0

    code, history = _get(base + "/history?token=s3cret", token="s3cret")
    assert code == 200
    assert history["jobs"][0]["name"] == "Movie (2019).mp4"
    assert history["jobs"][0]["srt"] is True


def test_failure_path(server):
    _, base, state = server
    state.begin("Bad Disc", "dvd", series_mode=False, encoder="x265", jobs_total=1)
    state.fail("makemkvcon exited with code 1")
    code, status = _get(base + "/status", token="s3cret")
    assert code == 200
    assert status["stage"] == "error"
    assert status["last_error"].startswith("makemkvcon")
    assert not status["busy"]