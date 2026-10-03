"""Stream-server lifecycle + probe-stream CLI tests (v0.2.17 step 1).

All offline: `runner` fakes the makemkvcon spawn, `_port_free`/`_http_ok`
are monkeypatched at the socket/HTTP seams, and the CLI verdicts stub
the server + URL probe. No test needs makemkvcon, ffmpeg, a drive, or
a real socket.
"""

from __future__ import annotations

import io
import json
import subprocess
from collections import deque

import pytest

from bluray_to_mp4 import cli
from bluray_to_mp4 import streamsource
from bluray_to_mp4.makemkv import (
    BetaKeyExpiredError,
    DiscOpenError,
    scan_disc,
)
from bluray_to_mp4.streamsource import (
    StreamServer,
    StreamServerError,
    probe_title_url,
)
from tests.fixtures import MOVIE_DISC_OUTPUT, fake_runner


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

class _FakeProc:
    """Popen stand-in: running until terminate()/kill(), or preset rc."""

    def __init__(self, lines=(), *, rc=None, ignore_terminate=False):
        self.stdout = io.StringIO("".join(l + "\n" for l in lines))
        self.rc = rc                       # None = still running
        self.ignore_terminate = ignore_terminate
        self.terminated = 0
        self.killed = 0
        self.waits = 0

    def poll(self):
        return self.rc

    def terminate(self):
        self.terminated += 1
        if not self.ignore_terminate:
            self.rc = 0

    def kill(self):
        self.killed += 1
        self.rc = 0

    def wait(self, timeout=None):
        self.waits += 1
        if self.rc is None:
            if timeout is not None:
                raise subprocess.TimeoutExpired("makemkvcon", timeout)
            return 0
        return self.rc


class _FakeServer:
    """Quacks like StreamServer for the CLI verdict tests."""

    def __init__(self, port=51000):
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        self.disc_index = 0
        self.stdout_tail = deque()
        self.stop_calls = 0
        self.stopped = False

    def stop(self, timeout=10.0):
        self.stop_calls += 1
        self.stopped = True


def _ready_seams(monkeypatch, http=lambda url, timeout=2.0: True,
                 port_free=lambda ip, port: True):
    monkeypatch.setattr(streamsource, "_port_free", port_free)
    monkeypatch.setattr(streamsource, "_http_ok", http)
    monkeypatch.setattr(streamsource, "READY_INITIAL_DELAY", 0.0)
    monkeypatch.setattr(streamsource, "READY_POLL_INTERVAL", 0.0)


def _live_before():
    return list(streamsource._LIVE_SERVERS)


# --------------------------------------------------------------------------
# start_stream_server: argument shape per usage.txt (design §1.3)
# --------------------------------------------------------------------------

def test_start_args_match_usage_txt_stream_form(monkeypatch):
    """Robot mode + stream command + loopback bind + --cache=128 +
    -stdout messages + disc parameter, options before the command."""
    _ready_seams(monkeypatch)
    calls = []

    def runner(binary, args):
        calls.append((binary, list(args)))
        return _FakeProc()

    server = streamsource.start_stream_server(0, runner=runner)
    try:
        assert calls == [(
            "makemkvcon",
            ["-r", "stream", "--cache=128", "--bindip=127.0.0.1",
             "--bindport=51000", "--messages=-stdout"],
        )]
        assert server.port == 51000
        assert server.base_url == "http://127.0.0.1:51000"
        assert server.disc_index == 0
    finally:
        server.stop()


def test_start_binds_loopback_only_by_default(monkeypatch):
    """The server has no auth (design §1.3): loopback-only binding IS
    the security model, and it is the default."""
    _ready_seams(monkeypatch)
    server = streamsource.start_stream_server(3, runner=lambda b, a: _FakeProc())
    try:
        assert server.base_url == "http://127.0.0.1:51000"
        assert server.disc_index == 3
    finally:
        server.stop()


def test_start_refuses_non_loopback_bind():
    """The server has no auth; binding it to the LAN must be refused."""
    with pytest.raises(StreamServerError) as exc:
        streamsource.start_stream_server(0, bind_ip="0.0.0.0")
    assert "loopback" in str(exc.value)


# --------------------------------------------------------------------------
# Port probing (design §7.1)
# --------------------------------------------------------------------------

def test_port_collision_tries_next_port(monkeypatch):
    attempted = []

    def port_free(ip, port):
        attempted.append(port)
        return port != 51000

    _ready_seams(monkeypatch, port_free=port_free)
    calls = []

    def runner(binary, args):
        calls.append(list(args))
        return _FakeProc()

    server = streamsource.start_stream_server(0, runner=runner)
    try:
        assert attempted == [51000, 51001]
        assert server.port == 51001
        assert "--bindport=51001" in calls[0]
    finally:
        server.stop()


def test_all_ports_full_raises_naming_range(monkeypatch):
    monkeypatch.setattr(streamsource, "_port_free", lambda ip, port: False)
    before = _live_before()
    with pytest.raises(StreamServerError) as exc:
        streamsource.start_stream_server(0)
    assert "51000-51009" in str(exc.value)
    assert "stream server" in str(exc.value)
    assert streamsource._LIVE_SERVERS == before  # nothing registered/leaked


# --------------------------------------------------------------------------
# Readiness / health check (design §1.4)
# --------------------------------------------------------------------------

def test_ready_polls_index_url_until_http_200(monkeypatch):
    polls = []

    def flaky(url, timeout=2.0):
        polls.append(url)
        return len(polls) >= 3

    _ready_seams(monkeypatch, http=flaky)
    server = streamsource.start_stream_server(0, runner=lambda b, a: _FakeProc())
    try:
        assert len(polls) == 3
        assert all(u == "http://127.0.0.1:51000/" for u in polls)
    finally:
        server.stop()


def test_not_ready_timeout_raises_typed_error_with_tail(monkeypatch):
    """Never-listening server -> StreamServerError carrying the stdout
    tail, and the child is terminated (no leak)."""
    _ready_seams(monkeypatch, http=lambda url, timeout=2.0: False)
    monkeypatch.setattr(streamsource, "READY_TIMEOUT", 0.05)
    proc = _FakeProc(lines=['MSG:1005,0,1,"MakeMKV started"'])
    before = _live_before()
    with pytest.raises(StreamServerError) as exc:
        streamsource.start_stream_server(0, runner=lambda b, a: proc)
    assert "not ready" in str(exc.value)
    assert "MakeMKV started" in str(exc.value)      # tail context present
    assert proc.terminated == 1                     # reaped: no leak
    assert streamsource._LIVE_SERVERS == before


def test_died_before_ready_maps_beta_key_error(monkeypatch):
    _ready_seams(monkeypatch)
    proc = _FakeProc(rc=1, lines=[
        'MSG:5055,0,0,"Evaluation period has expired","Evaluation period expired"',
    ])
    before = _live_before()
    with pytest.raises(BetaKeyExpiredError):
        streamsource.start_stream_server(0, runner=lambda b, a: proc)
    assert streamsource._LIVE_SERVERS == before


def test_died_before_ready_maps_disc_open_error(monkeypatch):
    _ready_seams(monkeypatch)
    proc = _FakeProc(rc=1, lines=[
        'MSG:5010,0,0,"Failed to open disc","Failed to open disc"',
    ])
    with pytest.raises(DiscOpenError):
        streamsource.start_stream_server(0, runner=lambda b, a: proc)


def test_died_before_ready_generic_error_has_exit_code(monkeypatch):
    _ready_seams(monkeypatch)
    proc = _FakeProc(rc=3)
    with pytest.raises(StreamServerError) as exc:
        streamsource.start_stream_server(0, runner=lambda b, a: proc)
    assert "code 3" in str(exc.value)


# --------------------------------------------------------------------------
# Leak safety (the hard rule from the orphan incident)
# --------------------------------------------------------------------------

def test_exception_during_ready_still_stops_child(monkeypatch):
    """Any exception escaping the readiness wait must reap the child."""
    def boom(url, timeout=2.0):
        raise RuntimeError("socket layer exploded")

    _ready_seams(monkeypatch, http=boom)
    proc = _FakeProc()
    with pytest.raises(RuntimeError):
        streamsource.start_stream_server(0, runner=lambda b, a: proc)
    assert proc.terminated == 1


def test_stop_terminates_then_kills_on_expiry():
    """terminate -> wait(10) -> kill budget, mirroring the encode path."""
    proc = _FakeProc(ignore_terminate=True)
    server = StreamServer(
        proc=proc, port=51000, base_url="http://127.0.0.1:51000", disc_index=0)
    server.stop(timeout=0.01)
    assert proc.terminated == 1
    assert proc.killed == 1
    assert proc.waits == 2     # timed wait raised, then kill() reaped


def test_stop_is_idempotent():
    proc = _FakeProc()
    server = StreamServer(
        proc=proc, port=51000, base_url="http://127.0.0.1:51000", disc_index=0)
    server.stop()
    server.stop()              # safe to call twice
    assert proc.terminated == 1
    assert proc.killed == 0


def test_atexit_cleanup_stops_live_servers():
    """Belt-and-suspenders: the atexit handler reaps every live server."""
    s1 = StreamServer(
        proc=_FakeProc(), port=51001, base_url="http://127.0.0.1:51001", disc_index=0)
    s2 = StreamServer(
        proc=_FakeProc(), port=51002, base_url="http://127.0.0.1:51002", disc_index=0)
    streamsource._LIVE_SERVERS.extend([s1, s2])
    streamsource._atexit_cleanup()
    assert s1.stopped and s2.stopped
    assert streamsource._LIVE_SERVERS == []


def test_stdout_tail_is_a_bounded_ring_buffer(monkeypatch):
    """§7.1: the daemon drain keeps at most 50 robot lines."""
    _ready_seams(monkeypatch)
    lines = [f'MSG:1005,0,1,"line {i}"' for i in range(60)]
    server = streamsource.start_stream_server(
        0, runner=lambda b, a: _FakeProc(lines=lines))
    try:
        server._drain.join(timeout=1.0)
        assert len(server.stdout_tail) == 50
        assert list(server.stdout_tail)[-1] == lines[-1]
    finally:
        server.stop()


# --------------------------------------------------------------------------
# Title URL shape (the one function that knows it; probe verifies it live)
# --------------------------------------------------------------------------

def test_probe_title_url_shape():
    server = StreamServer(
        proc=_FakeProc(), port=51000, base_url="http://127.0.0.1:51000", disc_index=0)
    assert probe_title_url(server, 0) == "http://127.0.0.1:51000/title0.ts"
    assert probe_title_url(server, 17) == "http://127.0.0.1:51000/title17.ts"


def test_stop_stream_server_module_alias_stops():
    proc = _FakeProc()
    server = StreamServer(
        proc=proc, port=51000, base_url="http://127.0.0.1:51000", disc_index=0)
    streamsource.stop_stream_server(server)
    assert proc.terminated == 1
    assert server.stopped


# --------------------------------------------------------------------------
# probe-stream CLI: verdicts, exit codes, cleanup
# --------------------------------------------------------------------------

def _green_probe(**kw):
    return cli._UrlProbe(
        True, "ffprobe", "mpegts", 7590.0,
        [("video", "hevc"), ("audio", "truehd"), ("subtitle", "pgs")],
        **kw)


def _run_cli(monkeypatch, capsys, server, probe, argv=("probe-stream", "0")):
    monkeypatch.setattr(cli, "scan_disc", lambda index: _cli_disc())
    monkeypatch.setattr(streamsource, "start_stream_server", lambda index: server)
    monkeypatch.setattr(cli, "_probe_url", probe)
    args = cli.build_parser().parse_args(list(argv))
    rc = args.func(args)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


_cli_disc_cache = {}


def _cli_disc():
    if "disc" not in _cli_disc_cache:
        _cli_disc_cache["disc"] = scan_disc(
            0, runner=fake_runner(MOVIE_DISC_OUTPUT.splitlines()))
    return _cli_disc_cache["disc"]


def test_cli_probe_stream_green(monkeypatch, capsys):
    server = _FakeServer()
    seen = {}

    def probe(url, timeout, ffprobe_bin="ffprobe", ffmpeg_bin="ffmpeg"):
        seen["url"], seen["timeout"] = url, timeout
        return _green_probe()

    rc, out, err = _run_cli(monkeypatch, capsys, server, probe)
    assert rc == 0
    assert "Verdict: GREEN" in out
    # The recommended title (main feature, tid 0) was probed at the
    # community-reported URL shape, with the default 60 s timeout.
    assert seen["url"] == "http://127.0.0.1:51000/title0.ts"
    assert seen["timeout"] == 60.0
    # Stream inventory + duration vs the scanned duration are reported.
    assert "Container: mpegts" in out
    assert "7590" in out
    assert "Video: 1 stream(s) - hevc" in out
    assert "Audio: 1 stream(s) - truehd" in out
    assert "Subtitles: 1 stream(s) - pgs" in out
    # Server stopped cleanly exactly once.
    assert server.stop_calls == 1
    assert "Stream server stopped." in out


def test_cli_probe_stream_yellow_when_no_subtitle(monkeypatch, capsys):
    server = _FakeServer()

    def probe(url, timeout, ffprobe_bin="ffprobe", ffmpeg_bin="ffmpeg"):
        return cli._UrlProbe(
            True, "ffprobe", "mpegts", 7590.0,
            [("video", "hevc"), ("audio", "truehd")])

    rc, out, err = _run_cli(monkeypatch, capsys, server, probe)
    assert rc == 0
    assert "Verdict: YELLOW" in out
    assert "NO subtitle" in out
    assert server.stop_calls == 1


def test_cli_probe_stream_red_when_url_unopenable(monkeypatch, capsys):
    server = _FakeServer()

    def probe(url, timeout, ffprobe_bin="ffprobe", ffmpeg_bin="ffmpeg"):
        return cli._UrlProbe(
            False, "failed", "", None, [],
            "ffprobe: exit code 1 | ffmpeg: exit code 1")

    rc, out, err = _run_cli(monkeypatch, capsys, server, probe)
    assert rc == 1
    assert "Verdict: RED" in out
    assert "exit code 1" in out
    assert server.stop_calls == 1


def test_cli_probe_stream_red_when_no_video_stream(monkeypatch, capsys):
    """URL opens but serves no video: not usable for encoding -> RED."""
    server = _FakeServer()

    def probe(url, timeout, ffprobe_bin="ffprobe", ffmpeg_bin="ffmpeg"):
        return cli._UrlProbe(
            True, "ffmpeg null read", "", None,
            [("audio", "aac")])

    rc, out, err = _run_cli(monkeypatch, capsys, server, probe)
    assert rc == 1
    assert "Verdict: RED" in out
    assert "no video" in out


def test_cli_probe_stream_red_when_server_misbehaves(monkeypatch, capsys):
    def broken_start(index):
        raise StreamServerError(
            "stream server at http://127.0.0.1:51000 was not ready")

    monkeypatch.setattr(cli, "scan_disc", lambda index: _cli_disc())
    monkeypatch.setattr(streamsource, "start_stream_server", broken_start)
    args = cli.build_parser().parse_args(["probe-stream", "0"])
    rc = args.func(args)
    captured = capsys.readouterr()
    assert rc == 1
    assert "Verdict: RED" in captured.out
    assert "error:" in captured.err


def test_cli_probe_stream_operational_error_exit_2(monkeypatch, capsys):
    """A dead makemkvcon/beta-key/disc failure never tested the server:
    exit 2 (operational), not a verdict."""
    def broken_start(index):
        raise BetaKeyExpiredError("MakeMKV's evaluation period has expired.")

    monkeypatch.setattr(cli, "scan_disc", lambda index: _cli_disc())
    monkeypatch.setattr(streamsource, "start_stream_server", broken_start)
    args = cli.build_parser().parse_args(["probe-stream", "0"])
    rc = args.func(args)
    captured = capsys.readouterr()
    assert rc == 2
    assert "evaluation period" in captured.err


def test_cli_probe_stream_scan_failure_exit_2(monkeypatch, capsys):
    def broken_scan(index):
        raise DiscOpenError("MakeMKV could not open the disc.")

    monkeypatch.setattr(cli, "scan_disc", broken_scan)
    args = cli.build_parser().parse_args(["probe-stream", "0"])
    rc = args.func(args)
    captured = capsys.readouterr()
    assert rc == 2
    assert "could not open" in captured.err


def test_cli_probe_stream_title_override_probes_given_tid(monkeypatch, capsys):
    server = _FakeServer()
    seen = {}

    def probe(url, timeout, ffprobe_bin="ffprobe", ffmpeg_bin="ffmpeg"):
        seen["url"] = url
        return _green_probe()

    rc, out, err = _run_cli(
        monkeypatch, capsys, server, probe,
        argv=("probe-stream", "0", "--title", "2"))
    assert rc == 0
    assert seen["url"] == "http://127.0.0.1:51000/title2.ts"
    assert "Probing title 2" in out


def test_cli_probe_stream_unknown_title_exit_2(monkeypatch, capsys):
    server = _FakeServer()
    rc, out, err = _run_cli(
        monkeypatch, capsys, server,
        lambda url, timeout, ffprobe_bin="ffprobe", ffmpeg_bin="ffmpeg": _green_probe(),
        argv=("probe-stream", "0", "--title", "99"))
    assert rc == 2
    assert "not on this disc" in err


def test_cli_probe_stream_parser_defaults():
    args = cli.build_parser().parse_args(["probe-stream", "0"])
    assert args.index == 0
    assert args.title is None
    assert args.timeout == 60.0
    assert args.func is cli.cmd_probe_stream


def test_cli_probe_stream_parser_overrides():
    args = cli.build_parser().parse_args(
        ["probe-stream", "1", "--title", "3", "--timeout", "30"])
    assert args.index == 1
    assert args.title == 3
    assert args.timeout == 30.0