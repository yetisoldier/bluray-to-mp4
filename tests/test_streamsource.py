"""Stream-server lifecycle + probe-stream CLI tests (v0.2.17 steps 1+4).

All offline: `runner` fakes the makemkvcon spawn, `_port_free`/`_http_ok`
are monkeypatched at the socket/HTTP seams, and the CLI verdicts stub
the server + URL probe. No test needs makemkvcon, ffmpeg, a drive, or
a real socket.

probe.4: start_stream_server walks an ordered argument-variant matrix
(A/B/C/D); a variant that exits before ready is recorded as a
StreamAttempt and the next shape is tried, and stderr is drained into
its own ring buffer (probe.3 discarded it with DEVNULL).
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
    StreamAttempt,
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

    def __init__(self, lines=(), *, rc=None, ignore_terminate=False,
                 stderr=None, stderr_lines=()):
        self.stdout = io.StringIO("".join(l + "\n" for l in lines))
        # probe.4: None = no stderr pipe (the DEVNULL fallback shape);
        # stderr_lines builds a fake stderr pipe for the drain tests.
        self.stderr = (
            io.StringIO("".join(l + "\n" for l in stderr_lines))
            if stderr is None and stderr_lines else stderr
        )
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
        self.variant = "A"               # probe.4: which argv shape won
        self.attempts = []               # probe.4: variants that died first
        self.stdout_tail = deque()
        self.stderr_tail = deque()       # probe.4: captured stderr tail
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
# start_stream_server: argument shape per usage.txt (design §1.3, probe.4 A)
# --------------------------------------------------------------------------

def test_start_args_match_usage_txt_stream_form(monkeypatch):
    """Variant A (attempt 1 of the probe.4 matrix) is the pinned argv
    shape: robot mode + stream command + --cache=128 + loopback bind
    + -stdout messages, options before the command, NO disc parameter.
    Pinned so a silent flag change (e.g. --upnp sneaking in) fails
    loudly instead of shipping unnoticed."""
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
        assert server.variant == "A"     # won on the first matrix try
        assert server.attempts == []     # nothing died before it
    finally:
        server.stop()


# --------------------------------------------------------------------------
# probe.4: argument-variant matrix (A dies -> B -> C -> D)
# --------------------------------------------------------------------------

def test_matrix_all_die_carries_four_attempt_records(monkeypatch):
    """probe.4: A dies -> B tried -> C -> D -> all die: the raised
    StreamServerError carries 4 StreamAttempt records (name, argv,
    exit code, drained tails), and every dead child was reaped."""
    _ready_seams(monkeypatch)
    procs = []

    def runner(binary, args):
        proc = _FakeProc(
            rc=4294967294,  # Eric's probe.3 hardware exit code (= -2)
            lines=['MSG:1005,0,1,"MakeMKV started","%1 started","MakeMKV"'],
            stderr_lines=["Error: failed to start the stream server"],
        )
        procs.append(proc)
        return proc

    before = _live_before()
    with pytest.raises(StreamServerError) as exc:
        streamsource.start_stream_server(0, runner=runner)
    records = exc.value.attempts
    assert [r.name for r in records] == ["A", "B", "C", "D"]
    assert len(procs) == 4                    # every variant spawned a child
    assert all(r.exit_code == 4294967294 for r in records)
    # The argv shapes are pinned: A robot+bind, B official example,
    # C bare defaults, D the original probe.1 robot+disc form.
    assert records[0].args == [
        "-r", "stream", "--cache=128", "--bindip=127.0.0.1",
        "--bindport=51000", "--messages=-stdout"]
    assert records[1].args == [
        "stream", "--upnp=1", "--cache=128", "--bindip=127.0.0.1",
        "--bindport=51000", "--messages=-stdout"]
    assert records[2].args == ["stream", "--messages=-stdout"]
    assert records[3].args == [
        "-r", "stream", "--cache=128", "--bindip=127.0.0.1",
        "--bindport=51000", "--messages=-stdout", "disc:0"]
    # Drained tails rode along on every record.
    assert "MakeMKV started" in records[0].stdout_tail
    assert records[0].stderr_tail == "Error: failed to start the stream server"
    # Message summarizes the matrix; nothing leaked into the live list.
    assert "A, B, C, D" in str(exc.value)
    assert streamsource._LIVE_SERVERS == before


def test_matrix_a_dies_b_ready_uses_variant_b(monkeypatch):
    """probe.4: A exits before ready -> B answers HTTP 2xx -> the
    server carries variant 'B' (and A's failure record), stays usable,
    and B's child is reaped on stop."""
    _ready_seams(monkeypatch)
    calls = []
    procs = []

    def runner(binary, args):
        calls.append(list(args))
        # First spawn (A) is born dead; every later one stays running.
        proc = _FakeProc(rc=-2 if not procs else None)
        procs.append(proc)
        return proc

    server = streamsource.start_stream_server(0, runner=runner)
    try:
        assert server.variant == "B"
        assert [r.name for r in server.attempts] == ["A"]
        assert server.attempts[0].exit_code == -2
        # The winning B spawn used the official example shape.
        assert calls[1] == [
            "stream", "--upnp=1", "--cache=128", "--bindip=127.0.0.1",
            "--bindport=51000", "--messages=-stdout"]
        # Usable as today: URL building works on the ready server.
        assert probe_title_url(server, 7) == "http://127.0.0.1:51000/title7.ts"
        assert procs[1].terminated == 0   # B's child lives while in use
    finally:
        server.stop()
    assert procs[0].terminated == 0      # A died on its own: no terminate
    assert procs[1].terminated == 1      # B reaped by stop(): no leak


def test_matrix_typed_error_propagates_without_next_variant(monkeypatch):
    """An argv shape cannot fix a beta key or a dead drive: the typed
    error propagates immediately instead of burning the matrix."""
    _ready_seams(monkeypatch)
    calls = []

    def runner(binary, args):
        calls.append(list(args))
        return _FakeProc(rc=1, lines=[
            'MSG:5055,0,0,"Evaluation period has expired","expired"'])

    with pytest.raises(BetaKeyExpiredError):
        streamsource.start_stream_server(0, runner=runner)
    assert len(calls) == 1              # variant A only: no B/C/D tries


def test_matrix_live_process_timeout_propagates_without_retry(monkeypatch):
    """A variant that stays ALIVE but never answers is a hang, not an
    argv problem: the not-ready timeout propagates without spawning
    the next shapes on top of it."""
    _ready_seams(monkeypatch, http=lambda url, timeout=2.0: False)
    monkeypatch.setattr(streamsource, "READY_TIMEOUT", 0.05)
    calls = []

    def runner(binary, args):
        calls.append(list(args))
        return _FakeProc()              # stays running, never answers

    before = _live_before()
    with pytest.raises(StreamServerError) as exc:
        streamsource.start_stream_server(0, runner=runner)
    assert "not ready" in str(exc.value)
    assert len(calls) == 1
    assert exc.value.attempts == []      # died-before-ready records only
    assert streamsource._LIVE_SERVERS == before


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
# probe.4: stderr capture (probe.3 discarded it with DEVNULL)
# --------------------------------------------------------------------------

def test_stderr_drain_fills_stderr_tail(monkeypatch):
    """The second drain thread consumes the stderr pipe: the real
    makemkvcon error line for an instant-exit failure may only exist
    there (probe.3's report showed only DRV inventory lines)."""
    _ready_seams(monkeypatch)
    server = streamsource.start_stream_server(0, runner=lambda b, a: _FakeProc(
        stderr_lines=["Error: failed to start the stream server"]))
    try:
        server._drain_err.join(timeout=1.0)
        assert list(server.stderr_tail) == \
            ["Error: failed to start the stream server"]
    finally:
        server.stop()


def test_stderr_tail_bounded_to_char_budget(monkeypatch):
    """The stderr ring keeps the NEWEST ~2000 chars: a chatty stderr
    can never deadlock the child on a full pipe."""
    _ready_seams(monkeypatch)
    lines = [f"err {i} " + "x" * 50 for i in range(100)]
    server = streamsource.start_stream_server(0, runner=lambda b, a: _FakeProc(
        stderr_lines=lines))
    try:
        server._drain_err.join(timeout=1.0)
        total = sum(len(l) for l in server.stderr_tail)
        assert total <= streamsource.STDERR_TAIL_CHARS
        assert list(server.stderr_tail)[-1] == lines[-1]  # newest kept
    finally:
        server.stop()


def test_tail_excerpt_default_limit_is_2000_chars():
    """probe.4: the excerpt default grew ~300 -> ~2000 chars so the
    real error line cannot scroll past the embedded report context."""
    tail = deque(["x" * 1000, "y" * 1000, "z" * 1000])
    excerpt = streamsource._tail_excerpt(tail)
    assert len(excerpt) == 2000
    assert excerpt.startswith("y") and excerpt.endswith("z")


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


def test_cli_probe_stream_red_prints_attempt_matrix_and_stderr(monkeypatch, capsys):
    """probe.4: a total server failure prints the whole try matrix -
    'Attempt A'..'Attempt D' argv summaries, exit codes, and the drained
    stdout ('out:')/stderr ('err:') tails - before the RED verdict."""
    records = [
        StreamAttempt("A", [
            "-r", "stream", "--cache=128", "--bindip=127.0.0.1",
            "--bindport=51000", "--messages=-stdout"],
            4294967294,
            'DRV:0,2,999,12,"BD-RE ASUS BW-16D1HT"',
            "Fatal: invalid arguments"),
        StreamAttempt("B", [
            "stream", "--upnp=1", "--cache=128", "--bindip=127.0.0.1",
            "--bindport=51000", "--messages=-stdout"],
            4294967294, "", "Fatal: invalid arguments"),
        StreamAttempt("C", ["stream", "--messages=-stdout"],
            4294967294, "", "Error: cannot open server port"),
        StreamAttempt("D", [
            "-r", "stream", "--cache=128", "--bindip=127.0.0.1",
            "--bindport=51000", "--messages=-stdout", "disc:0"],
            4294967294, "DRV:0,2,999,12", "Fatal: invalid arguments"),
    ]

    def broken_start(index):
        raise StreamServerError(
            "all 4 stream server argument variants (A, B, C, D) exited "
            "before becoming ready", attempts=records)

    monkeypatch.setattr(cli, "scan_disc", lambda index: _cli_disc())
    monkeypatch.setattr(streamsource, "start_stream_server", broken_start)
    args = cli.build_parser().parse_args(["probe-stream", "0"])
    rc = args.func(args)
    captured = capsys.readouterr()
    assert rc == 1
    # probe.4 build stamp identifies the report (probe.3 rule).
    assert cli.PROBE_ID == "probe.4"
    assert f"probe-stream {cli.PROBE_ID}" in captured.out
    # Every attempt line: argv summary -> exit code, then out:/err: tails.
    for name in ("A", "B", "C", "D"):
        assert f"Attempt {name}:" in captured.out
        assert f"-> exit 4294967294" in captured.out
    assert "out:" in captured.out
    assert "err:" in captured.out
    assert "Fatal: invalid arguments" in captured.out
    assert "DRV:0,2,999,12" in captured.out
    assert "Verdict: RED" in captured.out


def test_cli_probe_stream_green_notes_ready_variant(monkeypatch, capsys):
    """probe.4: the GREEN path notes which argument variant went
    ready, plus the attempts that died before it."""
    server = _FakeServer()
    server.variant = "B"
    server.attempts = [StreamAttempt(
        "A", ["-r", "stream", "--cache=128", "--bindip=127.0.0.1",
               "--bindport=51000", "--messages=-stdout"],
        -2, "DRV:0,2,999,12", "Fatal: invalid arguments")]

    rc, out, err = _run_cli(
        monkeypatch, capsys, server,
        lambda url, timeout, ffprobe_bin="ffprobe", ffmpeg_bin="ffmpeg": _green_probe())
    assert rc == 0
    assert "Verdict: GREEN" in out
    assert "Stream server ready via variant B" in out
    assert "Attempt A:" in out        # A's failure is still reported
    assert server.stop_calls == 1


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