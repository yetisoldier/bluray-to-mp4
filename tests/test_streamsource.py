"""Stream-server lifecycle + probe-stream CLI tests (v0.2.17 steps 1+4).

All offline: `runner` fakes the makemkvcon spawn, `_port_free`/`_http_ok`
are monkeypatched at the socket/HTTP seams, and the CLI verdicts stub
the server + URL probe. No test needs makemkvcon, ffmpeg, a drive, or
a real socket.

probe.4: start_stream_server walks an ordered argument-variant matrix
(A/B/C/D); a variant that exits before ready is recorded as a
StreamAttempt and the next shape is tried, and stderr is drained into
its own ring buffer (probe.3 discarded it with DEVNULL).

probe.5: every attempt records Eric's diagnostic outcome class
(classify_attempt_failure) plus the process disposition (exited vs
app-terminated vs alive); tails are the COMPLETE bounded buffers, a
MakeMKV version stamp rides in the stdout evidence, readiness
failures attach .timeout_attempt / .stream_attempt, and the CLI's
pass verdict is gated on a short ffmpeg encode proving decodable
video flows (survival is not a verdict).
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
    # probe.5: every record carries the process disposition (it died
    # itself) and Eric's outcome class derived from its own evidence
    # (no recognized signature -> the bare 'exit' class).
    for r in records:
        assert r.disposition == streamsource.PROCESS_EXITED
        assert r.classification == streamsource.OUTCOME_EXIT
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

    with pytest.raises(BetaKeyExpiredError) as exc:
        streamsource.start_stream_server(0, runner=runner)
    assert len(calls) == 1              # variant A only: no B/C/D tries
    # probe.5: the typed failure carries its single-attempt evidence
    # (argv, disposition, outcome class, whole tails) so the report
    # prints it even on the operational exit path.
    sa = getattr(exc.value, "stream_attempt", None)
    assert sa is not None
    assert sa.name == "A"
    assert sa.exit_code == 1
    assert sa.disposition == streamsource.PROCESS_EXITED
    assert "Evaluation period has expired" in sa.stdout_tail


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
    # probe.5: a readiness timeout is a fundamentally different
    # outcome from a self-exit - the child was ALIVE and WE terminated
    # it, so the evidence rides alone as .timeout_attempt with the
    # 'timeout' disposition and outcome class (exit code None).
    ta = exc.value.timeout_attempt
    assert ta is not None
    assert ta.name == "A"
    assert ta.exit_code is None
    assert ta.disposition == streamsource.PROCESS_TIMEOUT
    assert ta.classification == streamsource.OUTCOME_TIMEOUT
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
    # probe.5: the readiness timeout attaches its attempt evidence.
    assert exc.value.timeout_attempt is not None
    assert exc.value.timeout_attempt.disposition == \
        streamsource.PROCESS_TIMEOUT
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


def _run_cli(monkeypatch, capsys, server, probe, argv=("probe-stream", "0"),
             proof=(True, "")):
    monkeypatch.setattr(cli, "scan_disc", lambda index: _cli_disc())
    monkeypatch.setattr(streamsource, "start_stream_server", lambda index: server)
    monkeypatch.setattr(cli, "_probe_url", probe)
    # probe.5: the pass verdict is gated on the video-proof encode; the
    # CLI harness stubs the seam (its own tests fake ffmpeg below).
    monkeypatch.setattr(cli, "_prove_video_flows", lambda url, timeout, ffmpeg_bin="ffmpeg": proof)
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
    """probe.5: a total server failure prints the whole try matrix -
    'Attempt A'..'Attempt D' argv summaries, exit codes, Eric's outcome
    classes with inline meaning, dispositions, and the COMPLETE
    stdout/stderr tails - before the RED verdict."""
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
            4294967294, "", "Deprecated option ignored: --upnp"),
        StreamAttempt("C", ["stream", "--messages=-stdout"],
            4294967294, "", "Error: failed to open disc"),
        StreamAttempt("D", [
            "-r", "stream", "--cache=128", "--bindip=127.0.0.1",
            "--bindport=51000", "--messages=-stdout", "disc:0"],
            4294967294, "DRV:0,2,999,12", "Invalid command: stream"),
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
    # probe.5 build stamp identifies the report (probe.3 rule).
    assert cli.PROBE_ID == "probe.5"
    assert f"probe-stream {cli.PROBE_ID}" in captured.out
    # Every attempt line: argv summary -> exit code -> outcome class
    # -> disposition.
    for name in ("A", "B", "C", "D"):
        assert f"Attempt {name}:" in captured.out
    assert "-> exit 4294967294" in captured.out
    assert "[exit]" in captured.out               # A: no signature
    assert "[deprecated-option]" in captured.out   # B's stderr signature
    assert "[disc-source]" in captured.out         # C's stderr signature
    assert "[invalid-command]" in captured.out     # D's stderr signature
    assert "[exited itself]" in captured.out
    assert "rearranging arguments" in captured.out  # invalid-command hint
    assert "Fatal: invalid arguments" in captured.out
    assert "DRV:0,2,999,12" in captured.out
    assert "stderr: " in captured.out
    assert "stdout: " in captured.out
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


# --------------------------------------------------------------------------
# probe.5: Eric's diagnostic outcome classes (2026-10-02 19:49)
# --------------------------------------------------------------------------

def test_classify_invalid_command_signature():
    """'Invalid command: stream' means the executable rejects the
    command itself: rearranging arguments will not help."""
    assert streamsource.classify_attempt_failure(
        "", "Invalid command: stream",
        streamsource.PROCESS_EXITED) == streamsource.OUTCOME_INVALID_COMMAND
    assert streamsource.classify_attempt_failure(
        "Unknown command: stream", "",
        streamsource.PROCESS_EXITED) == streamsource.OUTCOME_INVALID_COMMAND


def test_classify_deprecated_option_signature():
    """'Deprecated option ignored' alone does not establish server
    support."""
    assert streamsource.classify_attempt_failure(
        "", "Deprecated option ignored: --upnp",
        streamsource.PROCESS_EXITED) == streamsource.OUTCOME_DEPRECATED_OPTION
    assert streamsource.classify_attempt_failure(
        "option --robot is obsolete", "",
        streamsource.PROCESS_EXITED) == streamsource.OUTCOME_DEPRECATED_OPTION


def test_classify_disc_source_signature():
    """Disc-open/drive/source phrasings - including the typed
    DiscOpenError message - map to the disc-source class."""
    for sig in ("MSG:5010,0,0,\"Failed to open disc\"",
                "Error: failed to open the disc",
                "unable to open source",
                "no disc in drive",
                "drive not found"):
        assert streamsource.classify_attempt_failure(
            "", sig,
            streamsource.PROCESS_EXITED) == streamsource.OUTCOME_DISC_SOURCE


def test_classify_exit_when_no_signature_matches():
    """A self-exit with no recognized signature is the bare 'exit'
    class - exit -2 alone does not identify the cause."""
    assert streamsource.classify_attempt_failure(
        "DRV:0,2,999,12", "",
        streamsource.PROCESS_EXITED) == streamsource.OUTCOME_EXIT
    assert streamsource.classify_attempt_failure(
        "", "",
        streamsource.PROCESS_EXITED) == streamsource.OUTCOME_EXIT


def test_classify_timeout_when_we_terminated_it():
    """A process WE killed after the readiness ceiling is 'timeout' -
    fundamentally different from a self-exit - even if the drained
    output carries a signature."""
    assert streamsource.classify_attempt_failure(
        "", "", streamsource.PROCESS_TIMEOUT) == streamsource.OUTCOME_TIMEOUT
    # The disposition wins over any signature text.
    assert streamsource.classify_attempt_failure(
        "", "Error: failed to open disc",
        streamsource.PROCESS_TIMEOUT) == streamsource.OUTCOME_TIMEOUT


def test_classify_alive_unverified_when_still_running():
    """Still running at give-up: alive-unverified, disposition wins."""
    assert streamsource.classify_attempt_failure(
        "", "", streamsource.PROCESS_ALIVE) == \
        streamsource.OUTCOME_ALIVE_UNVERIFIED
    assert streamsource.classify_attempt_failure(
        "Invalid command: stream", "Invalid command: stream",
        streamsource.PROCESS_ALIVE) == \
        streamsource.OUTCOME_ALIVE_UNVERIFIED


def test_all_six_outcome_constants_and_hints():
    """The six classes exist as constants, each with an inline hint
    (Eric's one-line reading), and dispositions are distinct from
    classes."""
    classes = {
        streamsource.OUTCOME_INVALID_COMMAND,
        streamsource.OUTCOME_DEPRECATED_OPTION,
        streamsource.OUTCOME_DISC_SOURCE,
        streamsource.OUTCOME_EXIT,
        streamsource.OUTCOME_TIMEOUT,
        streamsource.OUTCOME_ALIVE_UNVERIFIED,
    }
    assert len(classes) == 6
    assert set(streamsource.OUTCOME_HINTS) == classes
    for klass in classes:
        assert streamsource.OUTCOME_HINTS[klass].strip()
    dispositions = {streamsource.PROCESS_EXITED,
                    streamsource.PROCESS_TIMEOUT,
                    streamsource.PROCESS_ALIVE}
    assert len(dispositions) == 3
    # Deliberate vocabulary overlap, not a bug: the timeout outcome IS
    # the app-terminated disposition - one word, two namespaces (the
    # other two outcome classes rename their disposition).
    assert streamsource.PROCESS_TIMEOUT == streamsource.OUTCOME_TIMEOUT
    assert streamsource.PROCESS_EXITED != streamsource.OUTCOME_EXIT
    assert streamsource.PROCESS_ALIVE != streamsource.OUTCOME_ALIVE_UNVERIFIED


def test_makemkv_version_stamp_extraction():
    """The MSG:1005 'MakeMKV vN.N started' line in the drained stdout
    yields the exact executable version; no stamp -> ''."""
    assert streamsource.makemkv_version(
        'MSG:1005,0,1,"MakeMKV v1.17.8 win64 started"') == "1.17.8"
    assert streamsource.makemkv_version(
        "MakeMKV v1.17.8 win64 started") == "1.17.8"
    assert streamsource.makemkv_version("DRV:0,2,999,12") == ""
    assert streamsource.makemkv_version("") == ""


def test_attempt_records_carry_disposition_and_classification(monkeypatch):
    """start_stream_server stamps every recorded attempt with the
    disposition (exited itself vs app-terminated) and Eric's outcome
    class derived at record time."""
    _ready_seams(monkeypatch)

    def runner(binary, args):
        return _FakeProc(
            rc=4294967294,
            lines=['MSG:1005,0,1,"MakeMKV v1.17.6 win64 started"'],
            stderr_lines=["Error: failed to open disc"])

    with pytest.raises(StreamServerError) as exc:
        streamsource.start_stream_server(0, runner=runner)
    records = exc.value.attempts
    assert [r.name for r in records] == ["A", "B", "C", "D"]
    for r in records:
        assert r.disposition == streamsource.PROCESS_EXITED
        assert r.classification == streamsource.OUTCOME_DISC_SOURCE
        # Version stamp rides in the drained stdout evidence.
        assert streamsource.makemkv_version(r.stdout_tail) == "1.17.6"


def test_attempt_full_tails_not_400_char_excerpts(monkeypatch):
    """probe.5: attempt tails are the WHOLE bounded buffers (the
    records already keep ~2000 chars), never re-excerpted to 400."""
    _ready_seams(monkeypatch)
    long_line = "x" * 600

    def runner(binary, args):
        return _FakeProc(
            rc=-2, lines=[long_line], stderr_lines=[long_line])

    with pytest.raises(StreamServerError) as exc:
        streamsource.start_stream_server(0, runner=runner)
    for r in exc.value.attempts:
        assert len(r.stdout_tail) >= len(long_line)
        assert len(r.stderr_tail) >= len(long_line)
    # ATTEMPT_TAIL_CHARS now equals the full excerpt budget.
    assert streamsource.ATTEMPT_TAIL_CHARS == streamsource.TAIL_EXCERPT_CHARS


def test_probe_title_urls_export():
    """probe_title_urls enumerates several title URLs at once - the
    fast-mode pipeline's enumeration seam."""
    server = StreamServer(
        proc=_FakeProc(), port=51000, base_url="http://127.0.0.1:51000",
        disc_index=0)
    assert streamsource.probe_title_urls(server, [0, 1, 2]) == [
        "http://127.0.0.1:51000/title0.ts",
        "http://127.0.0.1:51000/title1.ts",
        "http://127.0.0.1:51000/title2.ts",
    ]
    assert streamsource.probe_title_urls(server, []) == []


# --------------------------------------------------------------------------
# probe.5: video-proves-success gate (fake ffprobe/ffmpeg seams only)
# --------------------------------------------------------------------------

def test_prove_video_flows_runs_short_ffmpeg_encode(monkeypatch):
    """The gate encodes 5 s off the URL through the _run_ffmpeg_read
    seam; rc 0 proves decodable video."""
    seen = {}

    def fake_read(url, timeout, binary="ffmpeg"):
        seen["url"], seen["timeout"] = url, timeout
        return 0, ""

    monkeypatch.setattr(cli, "_run_ffmpeg_read", fake_read)
    proved, detail = cli._prove_video_flows(
        "http://127.0.0.1:51000/title0.ts", 60.0)
    assert proved is True
    assert detail == ""
    assert seen["url"] == "http://127.0.0.1:51000/title0.ts"
    assert seen["timeout"] == 60.0


def test_prove_video_flows_failure_carries_excerpt(monkeypatch):
    monkeypatch.setattr(cli, "_run_ffmpeg_read",
                       lambda url, timeout, binary="ffmpeg": (1, "boom"))
    proved, detail = cli._prove_video_flows("http://x", 5.0)
    assert proved is False
    assert "boom" in detail


def test_prove_video_flows_never_raises(monkeypatch):
    def boom(url, timeout, binary="ffmpeg"):
        raise OSError("gone")

    monkeypatch.setattr(cli, "_run_ffmpeg_read", boom)
    proved, detail = cli._prove_video_flows("http://x", 5.0)
    assert proved is False
    assert "not runnable" in detail


def test_cli_green_requires_video_proof(monkeypatch, capsys):
    """Survival is not a verdict: ffprobe sees video+audio+subs, but
    the ffmpeg encode fails to prove decodable video -> RED."""
    server = _FakeServer()
    rc, out, err = _run_cli(
        monkeypatch, capsys, server,
        lambda url, timeout, ffprobe_bin="ffprobe", ffmpeg_bin="ffmpeg": _green_probe(),
        proof=(False, "ffmpeg: exit code 1"))
    assert rc == 1
    assert "Video proof: FAILED" in out
    assert "alive-unverified" in out
    assert "survival is not a verdict" in out
    assert "Verdict: RED" in out
    assert server.stop_calls == 1


def test_cli_green_notes_video_proof_and_winning_variant(monkeypatch, capsys):
    """The GREEN path notes the passed video proof and which variant
    won (here B, with A's failure block printed first)."""
    server = _FakeServer()
    server.variant = "B"
    server.attempts = [StreamAttempt(
        "A", ["-r", "stream", "--cache=128", "--bindip=127.0.0.1",
               "--bindport=51000", "--messages=-stdout"],
        -2, "DRV:0,2,999,12", "Fatal: invalid arguments")]
    rc, out, err = _run_cli(monkeypatch, capsys, server, lambda url, timeout, ffprobe_bin="ffprobe", ffmpeg_bin="ffmpeg": _green_probe())
    assert rc == 0
    assert "Stream server ready via variant B" in out
    assert "Attempt A:" in out
    assert "Video proof: decodable video flows" in out
    assert "Verdict: GREEN" in out


def test_cli_red_when_no_video_stream_seen(monkeypatch, capsys):
    """ffprobe sees no video stream: RED before the proof encode is
    even relevant."""
    server = _FakeServer()
    rc, out, err = _run_cli(
        monkeypatch, capsys, server,
        lambda url, timeout, ffprobe_bin="ffprobe", ffmpeg_bin="ffmpeg": cli._UrlProbe(
            True, "ffmpeg null read", "", None, [("audio", "aac")]))
    assert rc == 1
    assert "no video stream" in out
    assert "Verdict: RED" in out


def test_cli_red_timeout_attempt_prints_full_evidence(monkeypatch, capsys):
    """A readiness timeout prints its own single-attempt block with
    the timeout disposition - WE terminated a live child."""
    record = StreamAttempt(
        "A", ["-r", "stream", "--cache=128", "--bindip=127.0.0.1",
               "--bindport=51000", "--messages=-stdout"],
        None, 'MSG:1005,0,1,"MakeMKV v1.17.8 win64 started"', "",
        disposition=streamsource.PROCESS_TIMEOUT,
        classification=streamsource.OUTCOME_TIMEOUT)

    def broken_start(index):
        raise StreamServerError(
            "stream server at http://127.0.0.1:51000 was not ready "
            "within 30s", timeout_attempt=record)

    monkeypatch.setattr(cli, "scan_disc", lambda index: _cli_disc())
    monkeypatch.setattr(streamsource, "start_stream_server", broken_start)
    args = cli.build_parser().parse_args(["probe-stream", "0"])
    rc = args.func(args)
    captured = capsys.readouterr()
    assert rc == 1
    assert "Attempt A:" in captured.out
    assert "[timeout]" in captured.out
    assert "app-terminated" in captured.out
    assert "makemkv version: 1.17.8" in captured.out
    assert "Verdict: RED" in captured.out


def test_cli_operational_error_prints_stream_attempt_evidence(monkeypatch, capsys):
    """A typed single-attempt failure (beta key) prints its evidence
    block on the operational exit-2 path too."""
    record = StreamAttempt(
        "A", ["-r", "stream", "--cache=128"], 1,
        "MSG:5055,0,0,\"Evaluation period has expired\"", "")

    def broken_start(index):
        exc = BetaKeyExpiredError("MakeMKV's evaluation period has expired.")
        exc.stream_attempt = record
        raise exc

    monkeypatch.setattr(cli, "scan_disc", lambda index: _cli_disc())
    monkeypatch.setattr(streamsource, "start_stream_server", broken_start)
    args = cli.build_parser().parse_args(["probe-stream", "0"])
    rc = args.func(args)
    captured = capsys.readouterr()
    assert rc == 2
    assert "Attempt A:" in captured.out
    assert "evaluation period" in captured.out.lower()
    assert "error:" in captured.err