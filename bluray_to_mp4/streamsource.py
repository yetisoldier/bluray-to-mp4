"""MakeMKV stream-server lifecycle: spawn, probe-ready, never leak.

v0.2.17 design §1.3/§1.4/§7.1 (docs/designs/v0.2.17-stream-encode.md):
`makemkvcon stream` runs MakeMKV's built-in web server, so fast mode
can encode straight off the disc through per-title HTTP URLs with no
.mkv rip on disk. The official usage.txt documents only the options
(--upnp/--bindip/--bindport, --cache=128 recommended for streaming);
the per-title URL shape, served container, and subtitle presence are
community-reported until the `probe-stream` command (shipped alongside
this module) verifies them against a real disc - the staged rollout in
§11 gates the fast-mode toggle on that verdict.

Hard rule from the 2026-10-02 orphaned-makemkvcon incident (§7.1): the
server process is registered for termination in EVERY exit path - spawn
failure, death-before-ready, readiness timeout, caller cancel, and an
atexit belt-and-suspenders handler - so the server can never leak past
the app that started it.

Standard library only, fully testable offline: `runner` replaces the
spawn (tests assert the exact argument shape), `_port_free`/`_http_ok`
are the socket seams, and no test needs makemkvcon or a real drive.
"""

from __future__ import annotations

import atexit
import socket
import subprocess
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from .binaries import resolve_makemkvcon
from .makemkv import (
    BETA_EXPIRED_CODES,
    DISC_OPEN_ERROR,
    _BETA_HINT,
    BetaKeyExpiredError,
    DiscOpenError,
    MakeMkvError,
    parse_line,
)

__all__ = [
    "StreamServerError",
    "StreamServer",
    "start_stream_server",
    "stop_stream_server",
    "probe_title_url",
]

# usage.txt: --bindport default is 51000; design §7.1 probes 51000..51009.
DEFAULT_PORT_START = 51000
DEFAULT_PORT_END = 51009
TAIL_LINES = 50        # §7.1: bounded robot-stdout ring buffer
READY_TIMEOUT = 30.0   # §1.4: 30 s readiness ceiling
READY_INITIAL_DELAY = 0.1   # §1.4: 100 ms before the first poll
READY_POLL_INTERVAL = 0.5   # §1.4: 500 ms between polls
HTTP_POLL_TIMEOUT = 2.0     # per-attempt urlopen timeout
STOP_TIMEOUT = 10.0        # terminate -> wait(10) -> kill budget

# The stream server has NO authentication (§1.3); loopback-only binding
# IS the security model. Never 0.0.0.0, never the LAN.
_LOOPBACK_BINDS = {"127.0.0.1", "localhost", "::1"}

# Every live server, so the atexit handler can reap strays (§7.1).
_LIVE_SERVERS: list["StreamServer"] = []


class StreamServerError(MakeMkvError):
    """Stream server could not start, become ready, or behave.

    Subclasses MakeMkvError so existing generic handlers still catch
    it, while the CLI can single it out for the RED verdict (server
    misbehaves) versus the operational exit code (makemkvcon missing).
    """


@dataclass
class StreamServer:
    """Handle to one running `makemkvcon stream` web server."""

    proc: subprocess.Popen
    port: int
    base_url: str        # http://127.0.0.1:<port>
    disc_index: int
    # §7.1: daemon reader thread keeps this at the last 50 robot lines;
    # a chatty makemkvcon can never deadlock on a full pipe, and error
    # messages get real context instead of "(no output)".
    stdout_tail: deque[str] = field(
        default_factory=lambda: deque(maxlen=TAIL_LINES))
    stopped: bool = field(default=False, init=False, repr=False, compare=False)
    _drain: threading.Thread | None = field(
        default=None, init=False, repr=False, compare=False)

    def stop(self, timeout: float = STOP_TIMEOUT) -> None:
        """Terminate, wait(timeout), then kill on expiry. Idempotent.

        Safe to call twice and safe on an already-exited process; the
        drain thread is joined so stdout_tail is final afterwards.
        """
        if self.stopped:
            return
        self.stopped = True
        for i, s in enumerate(_LIVE_SERVERS):
            if s is self:
                del _LIVE_SERVERS[i]
                break
        proc = self.proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        # Reap the reader thread before closing the pipe so the ring
        # buffer is final (the process is dead: its stdout is at EOF).
        if self._drain is not None and self._drain.is_alive():
            self._drain.join(timeout=2.0)
        if proc is not None and proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass
        self._drain = None


def _drain_stdout(server: "StreamServer") -> None:
    """Reader-thread body: consume robot stdout into the ring buffer."""
    try:
        for raw in server.proc.stdout:
            server.stdout_tail.append(raw.rstrip("\r\n"))
    except (ValueError, OSError):
        pass  # pipe closed underneath us by stop(): nothing left to read


def _default_runner(binary: str, args: list[str]) -> subprocess.Popen:
    """Spawn makemkvcon with a drained stdout pipe (make_runner style)."""
    binary = resolve_makemkvcon(binary)
    try:
        return subprocess.Popen(
            [binary, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise MakeMkvError(
            "makemkvcon not found on PATH. Install MakeMKV from "
            "https://www.makemkv.com/download/ and restart the app."
        ) from exc


def _port_free(bind_ip: str, port: int) -> bool:
    """True when a probe socket can bind (§7.1: pick the port pre-spawn).

    A busy port - a leftover/foreign makemkvcon stream server included -
    simply moves us to the next candidate; we never touch the process
    that holds it.
    """
    family = socket.AF_INET6 if ":" in bind_ip else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.bind((bind_ip, port))
    except OSError:
        return False
    finally:
        sock.close()
    return True


def _http_ok(url: str, timeout: float = HTTP_POLL_TIMEOUT) -> bool:
    """True when the server answers with HTTP 2xx (§1.4 readiness)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= getattr(resp, "status", 0) < 300
    except Exception:
        # ConnectionRefusedError/URLError/HTTPError/socket.timeout all
        # mean "not ready yet" while the socket is still coming up.
        return False


def _tail_excerpt(tail: deque[str], limit: int = 300) -> str:
    text = " | ".join(tail)
    return text[-limit:] if len(text) > limit else text


def _classify_exit(server: "StreamServer") -> None:
    """Raise the typed failure for a server that died before ready.

    Robot MSG lines in the drained tail map onto the existing typed
    exceptions (§1.3): beta-key expiry (5021/5055) and disc-open failure
    (5010) reuse the scan-side errors; anything else is a generic
    StreamServerError carrying the tail for the report. The drain
    thread gets a moment to consume the pipe first: the process can be
    dead with MSG lines still in flight.
    """
    if server._drain is not None and server._drain.is_alive():
        server._drain.join(timeout=0.5)
    for line in list(server.stdout_tail):
        try:
            kind, payload = parse_line(line)
        except (ValueError, IndexError):
            continue  # chatter that is not robot output: skip, not crash
        if kind == "MSG":
            code = payload[0]
            if code in BETA_EXPIRED_CODES:
                raise BetaKeyExpiredError(_BETA_HINT)
            if code == DISC_OPEN_ERROR:
                raise DiscOpenError(
                    "MakeMKV could not open the disc. Check the drive, "
                    "disc condition, and that the disc is a video "
                    "Blu-ray/DVD."
                )
    rc = server.proc.poll()
    raise StreamServerError(
        "makemkvcon stream server exited before becoming ready "
        f"(code {rc}); last output: "
        f"{_tail_excerpt(server.stdout_tail) or '(no server output)'}"
    )


def _wait_ready(server: "StreamServer", timeout: float) -> None:
    """Poll the index URL until 2xx, process death, or timeout (§1.4)."""
    url = server.base_url + "/"
    deadline = time.monotonic() + timeout
    time.sleep(READY_INITIAL_DELAY)
    while True:
        if server.proc.poll() is not None:
            _classify_exit(server)  # raises; never returns
        if _http_ok(url):
            return
        if time.monotonic() >= deadline:
            # Give the drain thread a moment so the embedded tail is
            # complete (the process may be mid-write when we give up).
            if server._drain is not None and server._drain.is_alive():
                server._drain.join(timeout=0.5)
            raise StreamServerError(
                f"stream server at {server.base_url} was not ready "
                f"within {timeout:.0f}s; last output: "
                f"{_tail_excerpt(server.stdout_tail) or '(no server output)'}"
            )
        time.sleep(READY_POLL_INTERVAL)


def start_stream_server(
    disc_index: int,
    *,
    binary: str = "makemkvcon",
    bind_ip: str = "127.0.0.1",
    port_start: int = DEFAULT_PORT_START,
    port_end: int = DEFAULT_PORT_END,
    runner: Callable[[str, list[str]], subprocess.Popen] | None = None,
) -> StreamServer:
    """Start a READY `makemkvcon stream` server for one disc (§1.3).

    Spawns `makemkvcon -r stream --cache=128 --bindip=<ip>
    --bindport=<port> --messages=-stdout disc:<index>` (robot mode so
    MSG lines stay parseable by parse_line; one server serves every
    title), polls the index URL until it answers (§1.4), and returns a
    ready StreamServer. Every failure path - non-loopback bind, no free
    port, spawn failure, death-before-ready, readiness timeout - stops
    the child before raising: the server can never leak.

    runner, when given, replaces the spawn seam (tests assert the
    argument shape); _port_free/_http_ok are the other test seams.
    """
    if bind_ip not in _LOOPBACK_BINDS:
        raise StreamServerError(
            f"refusing to bind the stream server to {bind_ip!r}: the "
            "server has no authentication and must stay loopback-only "
            "(127.0.0.1); never expose it to the LAN"
        )
    port = None
    for candidate in range(port_start, port_end + 1):
        if _port_free(bind_ip, candidate):
            port = candidate
            break
    if port is None:
        raise StreamServerError(
            f"no free port in {port_start}-{port_end}; a leftover "
            "makemkvcon stream server may be running - end it in Task "
            "Manager"
        )
    args = [
        "-r",
        "stream",
        "--cache=128",
        f"--bindip={bind_ip}",
        f"--bindport={port}",
        "--messages=-stdout",
        f"disc:{disc_index}",
    ]
    spawn = runner or _default_runner
    proc = spawn(binary, args)
    server = StreamServer(
        proc=proc,
        port=port,
        base_url=f"http://{bind_ip}:{port}",
        disc_index=disc_index,
    )
    _LIVE_SERVERS.append(server)
    drain = threading.Thread(
        target=_drain_stdout, args=(server,), daemon=True,
        name=f"b2m-stream-drain-{port}",
    )
    server._drain = drain
    drain.start()
    try:
        _wait_ready(server, READY_TIMEOUT)
    except BaseException:
        # The hard rule: no exit path leaves the child running.
        try:
            server.stop()
        except Exception:
            pass
        raise
    return server


def stop_stream_server(server: "StreamServer", *, timeout: float = STOP_TIMEOUT) -> None:
    """Module-level stop (design §2 naming parity); see StreamServer.stop."""
    server.stop(timeout=timeout)


def probe_title_url(server: "StreamServer", tid: int) -> str:
    """Per-title MPEG-TS URL (§1.1 best evidence: /title<N>.ts).

    The shape is community-reported, NOT officially documented; the
    probe-stream command exists precisely to verify it against a real
    disc before fast mode trusts it (§11 staged rollout). This is the
    only function that knows the URL shape.
    """
    return f"{server.base_url}/title{tid}.ts"


def _atexit_cleanup() -> None:
    """Stop every still-live server at process exit (§7.1 belt-and-suspenders)."""
    for server in list(_LIVE_SERVERS):
        try:
            server.stop()
        except Exception:
            pass


atexit.register(_atexit_cleanup)