"""Tests for the built-in monitoring API (token auth + endpoints)."""

import json
import threading
import urllib.request

import pytest

from bluray_to_mp4.monitor import (
    JobRecord,
    MonitorState,
    start_server,
    start_server_with_fallback,
)


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


def test_job_record_gb_per_hour_pins_formula():
    """QW-6: gb_per_hour uses decimal GB (1e9) per hour of video - the
    same unit as the encode size budgets (2.0 GB/hr Blu-ray, 1.0 GB/hr
    DVD) - and never divides by zero for unknown size or duration."""
    rec = JobRecord(name="Movie (2019).mp4", kind="movie",
                    duration_seconds=7200, encoder="x265",
                    size_bytes=2_000_000_000)
    # 2 GB across 2 h -> exactly 1.0 GB/hr.
    assert abs(rec.gb_per_hour - 1.0) < 1e-12
    # Different shape, same rate: 0.5 GB across half an hour.
    rec2 = JobRecord(name="E.mp4", kind="series", duration_seconds=1800,
                     encoder="nvenc", size_bytes=500_000_000)
    assert abs(rec2.gb_per_hour - 1.0) < 1e-12
    # Unknown duration or size reads as 0.0, never a crash.
    assert JobRecord(name="a", kind="movie", duration_seconds=0,
                     encoder="x", size_bytes=123).gb_per_hour == 0.0
    assert JobRecord(name="b", kind="movie", duration_seconds=60,
                     encoder="x").gb_per_hour == 0.0


def test_history_json_includes_size_and_rate(server):
    """The /history payload carries size_bytes + gb_per_hour so remote
    check-ins see the achieved size and rate, not just names."""
    _, base, state = server
    state.job_done(JobRecord(name="Movie (2019).mp4", kind="movie",
                             duration_seconds=7200, encoder="x265",
                             size_bytes=2_000_000_000))
    code, history = _get(base + "/history?token=***", token="s3cret")
    assert code == 200
    job = history["jobs"][0]
    assert job["size_bytes"] == 2_000_000_000
    assert job["gb_per_hour"] == 1.0


def test_job_record_carries_target_gbph(server):
    """v0.2.18: JobRecord carries the run's size target (0.0 = a
    pre-slider record) and /history includes it, so remote check-ins
    see the target-vs-achieved pair Eric validates by hand."""
    _, base, state = server
    state.job_done(JobRecord(
        name="Movie (2019).mp4", kind="movie",
        duration_seconds=7200, encoder="x265",
        size_bytes=2_000_000_000, target_gb_per_hour=2.25))
    code, history = _get(base + "/history?token=***", token="s3cret")
    assert code == 200
    job = history["jobs"][0]
    assert job["target_gb_per_hour"] == 2.25
    assert job["gb_per_hour"] == 1.0  # achieved: 2 GB across 2 h

    # Pre-slider records default to 0.0 and still serialize fine.
    state.job_done(JobRecord(name="Legacy.mp4", kind="movie",
                             duration_seconds=3600, encoder="x265"))
    code, history = _get(base + "/history?token=***", token="s3cret")
    assert code == 200
    assert history["jobs"][0]["target_gb_per_hour"] == 0.0


def test_snapshot_reports_stage_and_progress_ages():
    state = MonitorState()
    state.begin("Disc", "dvd", series_mode=False, encoder="nvenc", jobs_total=1)
    state.update(stage="Upscaling movie ...", current_job="Movie", progress=1.0)
    snap = state.snapshot()
    assert snap["stage_started_at"] is not None
    assert snap["progress_updated_at"] is not None
    assert snap["health"]["status"] == "running"


def test_classifier_marks_stale_busy_100_percent_as_stuck():
    from bluray_to_mp4.monitor import classify_snapshot

    snap = {
        "busy": True,
        "stage": "Upscaling movie ...",
        "current_job": "Movie",
        "jobs_done": 0,
        "jobs_total": 1,
        "progress": 1.0,
        "updated_at": 100.0,
        "progress_updated_at": 100.0,
        "stage_started_at": 90.0,
        "last_error": None,
    }
    health = classify_snapshot(snap, now=500.0, stale_seconds=300.0)
    assert health["status"] == "stuck"
    assert not health["ok"]
    assert any("100.0%" in reason for reason in health["reasons"])


def test_classifier_marks_no_initial_progress_as_stalled():
    from bluray_to_mp4.monitor import classify_snapshot

    snap = {
        "busy": True,
        "stage": "Ripping title 0 ...",
        "jobs_done": 0,
        "jobs_total": 1,
        "progress": 0.0,
        "updated_at": 500.0,
        "progress_updated_at": 100.0,
        "stage_started_at": 100.0,
        "last_error": None,
    }
    health = classify_snapshot(snap, now=500.0, stale_seconds=300.0)
    assert health["status"] == "stalled"
    assert any("no progress" in reason for reason in health["reasons"])


def test_classifier_allows_initial_upscale_chunk_to_run_longer():
    from bluray_to_mp4.monitor import classify_snapshot

    snap = {
        "busy": True,
        "stage": "Upscaling MAMMA_MIA (2018).mp4 (720p) ...",
        "current_job": "MAMMA_MIA (2018).mp4 (720p)",
        "jobs_done": 0,
        "jobs_total": 1,
        "progress": 0.0,
        "updated_at": 500.0,
        "progress_updated_at": 100.0,
        "stage_started_at": 100.0,
        "last_error": None,
    }
    health = classify_snapshot(snap, now=1100.0, stale_seconds=300.0)
    assert health["status"] == "running"
    assert health["ok"]


def test_lan_watcher_uses_monitor_health(server):
    from bluray_to_mp4.monitor_client import check_monitor, format_watch_result

    _, base, state = server
    state.begin("Disc", "dvd", series_mode=False, encoder="nvenc", jobs_total=1)
    state.update(stage="Upscaling movie ...", current_job="Movie", progress=1.0)
    with state._lock:
        state.updated_at = 100.0
        state.progress_updated_at = 100.0
        state.stage_started_at = 90.0

    result = check_monitor(base, token="s3cret", stale_seconds=300.0, now=500.0)
    assert result.reachable
    assert result.status == "stuck"
    assert not result.ok
    assert "STUCK: Movie" in format_watch_result(result)


def test_lan_watcher_reports_unreachable():
    from bluray_to_mp4.monitor_client import check_monitor, format_watch_result

    result = check_monitor("http://127.0.0.1:9", timeout=0.1)
    assert not result.reachable
    assert result.status == "unreachable"
    assert format_watch_result(result).startswith("UNREACHABLE:")


def test_start_server_with_fallback_tries_nearby_port(monkeypatch):
    import bluray_to_mp4.monitor as mon

    calls = []

    class DummyServer:
        server_address = ("127.0.0.1", 8766)

    def fake_start_server(**kwargs):
        calls.append(kwargs["port"])
        if kwargs["port"] == 8765:
            raise OSError(10013, "forbidden")
        return DummyServer(), "http://127.0.0.1:8766"

    monkeypatch.setattr(mon, "active_server", None)
    monkeypatch.setattr(mon, "start_server", fake_start_server)

    server, base, port = start_server_with_fallback(
        host="127.0.0.1", port=8765, token="s3cret", fallback_attempts=1)

    assert isinstance(server, DummyServer)
    assert base == "http://127.0.0.1:8766"
    assert port == 8766
    assert calls == [8765, 8766]


def test_start_server_with_fallback_clamps_walk_at_65535(monkeypatch):
    """v0.2.29 (Karen's validation): the port walk must never offer a
    candidate above 65535 - binding there raises OverflowError, which
    is NOT an OSError, so it would escape both this handler and the
    UI's except OSError and crash startup for a configured port near
    the top of the range. Instead the walk stops at 65535 and the
    first (real) bind error surfaces."""
    import bluray_to_mp4.monitor as mon

    calls = []

    def fake_start_server(**kwargs):
        calls.append(kwargs["port"])
        raise OSError(10013, "forbidden")  # every candidate is taken

    monkeypatch.setattr(mon, "active_server", None)
    monkeypatch.setattr(mon, "start_server", fake_start_server)

    # Port 65530 + 10 attempts would walk to 65540 without the clamp.
    with pytest.raises(OSError, match="forbidden"):
        start_server_with_fallback(
            host="127.0.0.1", port=65530, token="s3cret",
            fallback_attempts=10)
    assert calls == [65530, 65531, 65532, 65533, 65534, 65535]

    calls.clear()
    # At the boundary itself, 65535 is still tried (it is legal).
    with pytest.raises(OSError, match="forbidden"):
        start_server_with_fallback(
            host="127.0.0.1", port=65535, token="s3cret",
            fallback_attempts=3)
    assert calls == [65535]


def test_python_module_entry_routes_watch_monitor(monkeypatch):
    import sys

    import bluray_to_mp4.__main__ as module_main
    import bluray_to_mp4.cli as cli

    called = []
    monkeypatch.setattr(sys, "argv", ["python -m bluray_to_mp4", "watch-monitor"])
    monkeypatch.setattr(cli, "main", lambda: called.append(True) or 0)

    assert module_main.main() == 0
    assert called == [True]


def test_job_record_version_round_trip(server):
    """T-D3 (v0.2.35 §4.5 D.4): a JobRecord built with a version
    serializes through /history with the field present; a
    default-constructed record serializes "" (pre-v0.2.35 records stay
    renderable as an empty Ver cell, never a crash or a fabricated
    attribution)."""
    _, base, state = server
    state.job_done(JobRecord(
        name="Movie (2019).mp4", kind="movie",
        duration_seconds=7200, encoder="x265",
        version="0.2.35"))
    state.job_done(JobRecord(name="Legacy.mp4", kind="movie",
                            duration_seconds=3600, encoder="x265"))
    code, history = _get(base + "/history?token=***", token="s3cret")
    assert code == 200
    # History is newest-first: the second job_done lands at index 0.
    assert history["jobs"][0]["version"] == ""   # the legacy record
    assert history["jobs"][1]["version"] == "0.2.35"


def test_snapshot_and_health_carry_version(server):
    """v0.2.35 §4.5 D.2.3: BOTH /status and /health carry the build's
    version as a top-level field (both serve the snapshot); the value
    equals bluray_to_mp4.__version__ - the single source."""
    import bluray_to_mp4

    _, base, state = server
    state.begin("Disc", "dvd", series_mode=False, encoder="x265",
                jobs_total=1)
    code, health = _get(base + "/health")
    assert code == 200
    assert health["version"] == bluray_to_mp4.__version__
    code, status = _get(base + "/status", token="s3cret")
    assert status["version"] == bluray_to_mp4.__version__
