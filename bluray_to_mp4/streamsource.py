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

probe.4 (Eric, real-hardware run 2026-10-02 19:46): probe.3 STILL
exited with code 4294967294 (= -2) before the HTTP port answered, and
the report had two blind spots - the ~300-char stdout excerpt let the
real error line scroll away, and stderr was discarded outright. Now a
second drain thread captures stderr, both tails keep ~2000 chars, and
start_stream_server walks an ordered argument-variant matrix (A: robot
+ explicit bind, B: the official usage.txt example, C: bare defaults,
D: robot + disc), recording every variant that dies so the probe
report shows exactly which argv shape makemkvcon accepts.

probe.5 (Eric's diagnostic criteria, 2026-10-02 19:49): the argument
matrix is only useful as a diagnostic when the report DISTINGUISHES
the outcomes. classify_attempt_failure maps each attempt's evidence
onto Eric's classes - 'invalid-command' ("Invalid command: stream":
the executable rejects the command; rearranging arguments will not
help), 'deprecated-option' ("Deprecated option ignored": that alone
does not establish server support), 'disc-source' (investigate the
source argument, drive access, or disc opening), the bare 'exit' (it
died with no recognized signature - exit -2 alone does not identify
the cause), 'timeout' (WE terminated it - fundamentally different from
a self-exit), and 'alive-unverified' (still running when we gave up
waiting). Every StreamAttempt carries the COMPLETE drained stdout and
stderr (the buffers are already bounded at ~2000 chars - no 400-char
excerpts) plus the process disposition, and start_stream_server
attaches single-attempt evidence to typed failures and readiness
timeouts too. Success is defined by video, not survival: readiness
stays HTTP 2xx, but the probe-stream command only calls that a pass
after ffprobe identifies a video stream in the served title URL and a
short ffmpeg encode proves decodable video actually flows - survival
is not a verdict. probe_title_urls is the multi-title enumeration
seam the fast-mode pipeline will consume.

Standard library only, fully testable offline: `runner` replaces the
spawn (tests assert the exact argument shape), `_port_free`/`_http_ok`
are the socket seams, and no test needs makemkvcon or a real drive.
"""

from __future__ import annotations

import atexit
import re
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
    "StreamAttempt",
    "StreamServer",
    "start_stream_server",
    "stop_stream_server",
    "probe_title_url",
    "probe_title_urls",
    "classify_attempt_failure",
    "makemkv_version",
    "OUTCOME_INVALID_COMMAND",
    "OUTCOME_DEPRECATED_OPTION",
    "OUTCOME_DISC_SOURCE",
    "OUTCOME_EXIT",
    "OUTCOME_TIMEOUT",
    "OUTCOME_ALIVE_UNVERIFIED",
    "OUTCOME_HINTS",
    "PROCESS_EXITED",
    "PROCESS_TIMEOUT",
    "PROCESS_ALIVE",
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

# probe.4: the two probe.3 report blind spots are closed - stderr gets
# its own drain thread and char-bounded buffer, and tail excerpts keep
# ~2000 chars instead of ~300 (the real error line scrolled past).
STDERR_TAIL_CHARS = 2000    # stderr ring budget in characters
TAIL_EXCERPT_CHARS = 2000   # _tail_excerpt default (stdout + stderr)
# probe.5: per-attempt tails are the WHOLE bounded buffers - the
# report prints complete evidence, not 400-char excerpts.
ATTEMPT_TAIL_CHARS = TAIL_EXCERPT_CHARS
# probe.4 argument-variant matrix, tried in order until one is ready.
DEFAULT_ATTEMPTS = ["A", "B", "C", "D"]

# ---------------------------------------------------------------------------
# probe.5: Eric's diagnostic outcome classes (2026-10-02 19:49)
# ---------------------------------------------------------------------------
# The four argument attempts are useful as a diagnostic only when the
# report distinguishes these outcomes; exit -2 alone does not identify
# the cause.
OUTCOME_INVALID_COMMAND = "invalid-command"      # executable rejects the command
OUTCOME_DEPRECATED_OPTION = "deprecated-option"  # obsolete option ignored
OUTCOME_DISC_SOURCE = "disc-source"              # source/drive/disc opening
OUTCOME_EXIT = "exit"                            # died, no recognized signature
OUTCOME_TIMEOUT = "timeout"                      # WE terminated it (readiness ceiling)
OUTCOME_ALIVE_UNVERIFIED = "alive-unverified"   # running, video never verified

# Process dispositions at record time (probe.5): whether the child
# exited itself, the app terminated it after the readiness timeout, or
# it was still running when we gave up waiting - three fundamentally
# different outcomes that a bare exit code cannot tell apart.
PROCESS_EXITED = "exited"
PROCESS_TIMEOUT = "timeout"
PROCESS_ALIVE = "alive"

# Signature matchers for classify_attempt_failure: case-insensitive,
# searched over the attempt's stdout and stderr tails together.
_INVALID_COMMAND_RE = re.compile(
    r"invalid\s+command|unknown\s+command|unrecognized\s+command"
    r"|command\s+not\s+(?:found|recognized)|no\s+such\s+command",
    re.IGNORECASE,
)
_DEPRECATED_OPTION_RE = re.compile(
    r"(?:deprecated|obsolete)\s+(?:option|argument|flag|param)"
    r"|option\s+'?[\w.-]*'?\s+(?:is\s+)?(?:deprecated|obsolete)",
    re.IGNORECASE,
)
_DISC_SOURCE_RE = re.compile(
    r"msg:5010\b"                                   # typed disc-open MSG code
    r"|(?:failed|unable|could\s?not|cannot|can't|error)\s+(?:to\s+)?"
    r"open\s+(?:the\s+)?(?:disc|source)"            # the DiscOpenError phrasing
    r"|(?:disc|source)\s+open(?:ing)?\s+failed"
    r"|no\s+(?:disc|media)\b"
    r"|disc\s+(?:not\s+found|missing|unreadable|empty)"
    r"|drive\s+(?:not\s+(?:found|ready)|access\s+denied|error)"
    r"|(?:disc|source)\s+(?:error|unavailable)",
    re.IGNORECASE,
)

# The 'MakeMKV v(N) started' version stamp: the same pattern
# deps.check_makemkv parses from the scan output, reused so each
# probe.5 attempt block can name the exact executable version (the
# MSG:1005 started line rides in the drained stdout tails).
_MAKEMKV_VERSION_RE = re.compile(r"MakeMKV\s+v(\d+(?:\.\d+)*)")


def classify_attempt_failure(
    stdout_tail: str, stderr_tail: str, exited_vs_timeout: str
) -> str:
    """Map one attempt's evidence onto Eric's outcome classes (probe.5).

    'invalid-command' - "Invalid command: stream": the executable
      rejects the command; rearranging arguments will not help.
    'deprecated-option' - "Deprecated option ignored": a supplied
      option is obsolete; that alone does not establish server
      support.
    'disc-source' - disc-open/drive/source error phrasings (the
      typed DiscOpenError message included): investigate the source
      argument, drive access, or disc opening.
    'exit' - the process died itself with no recognized signature:
      the bare case (exit -2 alone does not identify the cause).
    'timeout' - WE terminated it after the readiness ceiling, which
      is fundamentally different from a self-exit.
    'alive-unverified' - still running when we gave up waiting.

    The disposition argument wins over any signature: a process we
    killed or one that is still alive is a different outcome from a
    self-exit no matter what the drained output happens to say.
    """
    if exited_vs_timeout == PROCESS_ALIVE:
        return OUTCOME_ALIVE_UNVERIFIED
    if exited_vs_timeout == PROCESS_TIMEOUT:
        return OUTCOME_TIMEOUT
    text = f"{stdout_tail or ''}\n{stderr_tail or ''}"
    if _INVALID_COMMAND_RE.search(text):
        return OUTCOME_INVALID_COMMAND
    if _DEPRECATED_OPTION_RE.search(text):
        return OUTCOME_DEPRECATED_OPTION
    if _DISC_SOURCE_RE.search(text):
        return OUTCOME_DISC_SOURCE
    return OUTCOME_EXIT


def makemkv_version(text: str) -> str:
    """'MakeMKV v1.17.8 win64 started' -> '1.17.8' ('' when absent).

    probe.5: the report stamps each attempt with the exact executable
    version, parsed from the MSG:1005 started lines in the drained
    stdout tails (same pattern as deps.check_makemkv).
    """
    m = _MAKEMKV_VERSION_RE.search(text or "")
    return m.group(1) if m else ""


# probe.5: Eric's own one-line reading of each outcome class, so the
# report's classification headline carries its meaning inline.
OUTCOME_HINTS = {
    OUTCOME_INVALID_COMMAND:
        "the executable rejects the command; rearranging arguments "
        "will not help",
    OUTCOME_DEPRECATED_OPTION:
        "a supplied option is obsolete; this alone does not establish "
        "server support",
    OUTCOME_DISC_SOURCE:
        "investigate the source argument, drive access, or disc opening",
    OUTCOME_EXIT:
        "the process died with no recognized failure signature; the "
        "exit code alone does not identify the cause",
    OUTCOME_TIMEOUT:
        "the process stayed silent past the readiness ceiling; the "
        "app terminated it",
    OUTCOME_ALIVE_UNVERIFIED:
        "the process was running but video was never verified",
}

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

    probe.4: `.attempts` carries one StreamAttempt per argument variant
    that exited before ready (empty for every other failure mode), so
    the probe report can print the whole try matrix.

    probe.5: `.timeout_attempt` carries the single variant that stayed
    ALIVE past the readiness ceiling until the app terminated it - the
    'timeout' outcome, fundamentally different from a self-exit - and
    typed spawn failures (disc open, beta key) get their evidence
    attached as `.stream_attempt` on the raised exception itself
    (BetaKeyExpiredError/DiscOpenError carry the attribute directly;
    this constructor accepts it uniformly for every shape).
    """

    def __init__(self, message: str, *,
                 attempts: "list[StreamAttempt] | None" = None,
                 timeout_attempt: "StreamAttempt | None" = None,
                 stream_attempt: "StreamAttempt | None" = None) -> None:
        super().__init__(message)
        self.attempts: "list[StreamAttempt]" = list(attempts or [])
        self.timeout_attempt: "StreamAttempt | None" = timeout_attempt
        self.stream_attempt: "StreamAttempt | None" = stream_attempt


@dataclass
class StreamAttempt:
    """One probe.5 argument-variant try: argv, outcome class, evidence.

    Recorded only when the spawned makemkvcon EXITED before its HTTP
    port answered - the case where the argv shape itself is the prime
    suspect. exit_code is makemkvcon's own code (4294967294 = -2 in
    Eric's probe.3 run). The tails are the COMPLETE drained stdout and
    stderr buffers (already bounded at ~TAIL_EXCERPT_CHARS = 2000
    chars upstream - printed whole in the report, never re-excerpted).

    probe.5: disposition says whether the process exited itself
    (PROCESS_EXITED), the app terminated it after the readiness
    timeout (PROCESS_TIMEOUT), or it was still running when we gave
    up (PROCESS_ALIVE) - three fundamentally different outcomes that
    a bare exit code cannot tell apart - and classification is Eric's
    diagnostic outcome (classify_attempt_failure) attached at record
    time. Hand-built records (tests) may leave classification empty;
    the report re-derives it from the tails then.
    """

    name: str                # 'A' | 'B' | 'C' | 'D'
    args: list[str]          # argv after the makemkvcon binary
    exit_code: int | None    # None only if the process never exited
    stdout_tail: str = ""   # complete drained stdout buffer (~2000 chars)
    stderr_tail: str = ""   # complete drained stderr buffer (~2000 chars)
    disposition: str = PROCESS_EXITED   # exited | timeout | alive (probe.5)
    classification: str = ""           # Eric's outcome class (probe.5)


@dataclass
class StreamServer:
    """Handle to one running `makemkvcon stream` web server."""

    proc: subprocess.Popen
    port: int
    base_url: str        # http://127.0.0.1:<port>
    disc_index: int
    # probe.4: which argument variant produced this ready server, plus
    # the recorded failures of the variants tried (and lost) before it.
    variant: str = "A"
    attempts: list[StreamAttempt] = field(
        default_factory=list, repr=False, compare=False)
    # §7.1: daemon reader thread keeps this at the last 50 robot lines;
    # a chatty makemkvcon can never deadlock on a full pipe, and error
    # messages get real context instead of "(no output)".
    stdout_tail: deque[str] = field(
        default_factory=lambda: deque(maxlen=TAIL_LINES))
    # probe.4: stderr is captured too (second drain thread), bounded to
    # the newest ~2000 CHARS - probe.3 discarded stderr entirely and the
    # real makemkvcon error line may live only there.
    stderr_tail: deque[str] = field(default_factory=deque)
    stopped: bool = field(default=False, init=False, repr=False, compare=False)
    _drain: threading.Thread | None = field(
        default=None, init=False, repr=False, compare=False)
    _drain_err: threading.Thread | None = field(
        default=None, init=False, repr=False, compare=False)

    def stop(self, timeout: float = STOP_TIMEOUT) -> None:
        """Terminate, wait(timeout), then kill on expiry. Idempotent.

        Safe to call twice and safe on an already-exited process; both
        drain threads are joined so the tails are final afterwards.
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
        # Reap BOTH reader threads (probe.4: stdout + stderr) before
        # closing the pipes so both ring buffers are final.
        for drain in (self._drain, self._drain_err):
            if drain is not None and drain.is_alive():
                drain.join(timeout=2.0)
        if proc is not None:
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass
        self._drain = None
        self._drain_err = None


def _drain_stdout(server: "StreamServer") -> None:
    """Reader-thread body: consume robot stdout into the ring buffer."""
    try:
        for raw in server.proc.stdout:
            server.stdout_tail.append(raw.rstrip("\r\n"))
    except (ValueError, OSError):
        pass  # pipe closed underneath us by stop(): nothing left to read


def _drain_stderr(server: "StreamServer") -> None:
    """Reader-thread body: consume stderr into the char-bounded buffer.

    probe.3 discarded stderr (DEVNULL); the makemkvcon error line for
    the instant-exit failures may only exist there. The buffer keeps
    the newest ~STDERR_TAIL_CHARS characters (at least one line), so a
    chatty stderr still can never deadlock on a full pipe.
    """
    try:
        stream = server.proc.stderr
        if stream is None:
            return  # spawn fell back to stderr=DEVNULL: nothing to read
        for raw in stream:
            server.stderr_tail.append(raw.rstrip("\r\n"))
            _trim_chars(server.stderr_tail, STDERR_TAIL_CHARS)
    except (ValueError, OSError):
        pass  # pipe closed underneath us by stop(): nothing left to read


def _trim_chars(tail: deque[str], max_chars: int) -> None:
    """Drop oldest lines until the buffer fits max_chars (keep >= 1 line)."""
    total = sum(len(line) for line in tail)
    while len(tail) > 1 and total > max_chars:
        total -= len(tail.popleft())


def _default_runner(binary: str, args: list[str]) -> subprocess.Popen:
    """Spawn makemkvcon with drained stdout AND stderr pipes (probe.4).

    stderr was DEVNULL through probe.3 - the real makemkvcon error line
    for the instant-exit failures may only exist there. If capturing
    both pipes ever fails at the OS level (fd exhaustion and friends),
    stdout PIPE is the priority: retry with stderr silenced rather
    than losing the robot stdout stream.
    """
    binary = resolve_makemkvcon(binary)
    argv = [binary, *args]
    not_found = (
        "makemkvcon not found on PATH. Install MakeMKV from "
        "https://www.makemkv.com/download/ and restart the app."
    )
    try:
        return subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (FileNotFoundError, PermissionError) as exc:
        raise MakeMkvError(not_found) from exc
    except OSError:
        # Both-PIPE spawn failed: stdout PIPE is the priority (above).
        try:
            return subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise MakeMkvError(not_found) from exc


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


def _tail_excerpt(tail: deque[str], limit: int = TAIL_EXCERPT_CHARS) -> str:
    """Join the tail lines, keeping the LAST `limit` chars (probe.4: 2000)."""
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


def _variant_argv(
    name: str, disc_index: int, bind_ip: str, port: int
) -> tuple[list[str], int, str]:
    """(args, port, base_url) for one probe.4 argument-variant try.

    A: robot mode + explicit loopback bind - the probe.3 shape that
       still exits with code 4294967294 (= -2) on Eric's hardware.
    B: the official usage.txt stream example - no -r, --upnp=1.
    C: bare `stream --messages=-stdout` - makemkvcon's own defaults
       (all addresses, port 51000). A diagnostic long shot, never a
       shape to ship: the server has no authentication.
    D: the original probe.1 shape - robot mode plus an explicit disc
       parameter. usage.txt documents no disc parameter for stream and
       removing it did NOT fix the exit, so D reproduces the original
       evidence inside the same report.
    """
    if name == "A":
        args = ["-r", "stream", "--cache=128",
                f"--bindip={bind_ip}", f"--bindport={port}",
                "--messages=-stdout"]
        return args, port, f"http://{bind_ip}:{port}"
    if name == "B":
        args = ["stream", "--upnp=1", "--cache=128",
                f"--bindip={bind_ip}", f"--bindport={port}",
                "--messages=-stdout"]
        return args, port, f"http://{bind_ip}:{port}"
    if name == "C":
        # Bare form: makemkvcon binds its own defaults (all addresses,
        # port 51000 per usage.txt).
        return (["stream", "--messages=-stdout"], DEFAULT_PORT_START,
                f"http://{bind_ip}:{DEFAULT_PORT_START}")
    if name == "D":
        args = ["-r", "stream", "--cache=128",
                f"--bindip={bind_ip}", f"--bindport={port}",
                "--messages=-stdout", f"disc:{disc_index}"]
        return args, port, f"http://{bind_ip}:{port}"
    raise StreamServerError(f"unknown stream server argument variant {name!r}")


def start_stream_server(
    disc_index: int,
    *,
    binary: str = "makemkvcon",
    bind_ip: str = "127.0.0.1",
    port_start: int = DEFAULT_PORT_START,
    port_end: int = DEFAULT_PORT_END,
    runner: Callable[[str, list[str]], subprocess.Popen] | None = None,
    attempts: list[str] | None = None,
) -> StreamServer:
    """Start a READY `makemkvcon stream` server for one disc (§1.3).

    probe.4: instead of betting the run on one argv shape, this walks
    the ordered argument-variant matrix (DEFAULT_ATTEMPTS: A, B, C, D -
    see _variant_argv). Each variant spawns its shape and polls its
    index URL; a variant that EXITS before ready is stopped, recorded
    as a StreamAttempt, and replaced by the next shape. The first
    variant to answer HTTP 2xx wins, and its StreamServer carries
    .variant plus the failed tries in .attempts. If every variant
    dies, the raised StreamServerError carries the whole attempt list
    on .attempts. Failures an argv shape cannot fix still propagate
    immediately: beta-key expiry, disc-open, spawn errors, and a live
    process that never answers (readiness timeout).

    Every failure path - non-loopback bind, no free port, spawn
    failure, death-before-ready, readiness timeout - stops the child
    before raising: the server can never leak.

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
    names = list(attempts) if attempts is not None else list(DEFAULT_ATTEMPTS)
    if not names:
        raise StreamServerError("no stream server argument variants to try")
    spawn = runner or _default_runner
    records: list[StreamAttempt] = []
    for name in names:
        args, variant_port, base_url = _variant_argv(
            name, disc_index, bind_ip, port)
        proc = spawn(binary, args)
        server = StreamServer(
            proc=proc,
            port=variant_port,
            base_url=base_url,
            disc_index=disc_index,
            variant=name,
            attempts=list(records),  # the variants that died before this one
        )
        _LIVE_SERVERS.append(server)
        drain = threading.Thread(
            target=_drain_stdout, args=(server,), daemon=True,
            name=f"b2m-stream-drain-{variant_port}",
        )
        drain_err = threading.Thread(
            target=_drain_stderr, args=(server,), daemon=True,
            name=f"b2m-stream-drain-err-{variant_port}",
        )
        server._drain = drain
        server._drain_err = drain_err
        drain.start()
        drain_err.start()
        try:
            _wait_ready(server, READY_TIMEOUT)
        except BaseException as exc:
            # The hard rule: no exit path leaves the child running.
            # The exit code is captured BEFORE stop() (terminate()
            # would overwrite it); the tails AFTER (stop() joins both
            # drain threads, so the ring buffers are final).
            exit_code = server.proc.poll()
            try:
                server.stop()
            except Exception:
                pass
            # probe.5: whatever happened, the COMPLETE evidence is
            # recorded first - the whole bounded tails plus the process
            # disposition (exited itself vs the app terminating a live
            # child) and Eric's outcome class derived from both.
            out_tail = _tail_excerpt(server.stdout_tail, ATTEMPT_TAIL_CHARS)
            err_tail = _tail_excerpt(server.stderr_tail, ATTEMPT_TAIL_CHARS)
            disposition = (PROCESS_EXITED if exit_code is not None
                           else PROCESS_TIMEOUT)
            attempt = StreamAttempt(
                name=name,
                args=list(args),
                exit_code=exit_code,
                stdout_tail=out_tail,
                stderr_tail=err_tail,
                disposition=disposition,
                classification=classify_attempt_failure(
                    out_tail, err_tail, disposition),
            )
            if exit_code is not None:
                # This argv shape died before ready: record it and let
                # the next variant try. Typed failures (beta key, disc
                # open) cannot be fixed by any argv shape: the same
                # evidence rides on the exception itself instead.
                if isinstance(exc, StreamServerError):
                    records.append(attempt)
                    continue
                if isinstance(exc, MakeMkvError):
                    exc.stream_attempt = attempt
                raise
            if isinstance(exc, StreamServerError):
                # Readiness timeout: the child was ALIVE and WE
                # terminated it - a fundamentally different outcome
                # from a self-exit, so it never joins the died-before-
                # ready records; it rides alone as .timeout_attempt.
                exc.timeout_attempt = attempt
            raise
        return server
    last = records[-1]
    raise StreamServerError(
        f"all {len(records)} stream server argument variants "
        f"({', '.join(rec.name for rec in records)}) exited before "
        f"becoming ready; last attempt {last.name} exited with code "
        f"{last.exit_code}"
        + (f"; last output: {last.stderr_tail or last.stdout_tail}"
           if (last.stderr_tail or last.stdout_tail) else ""),
        attempts=records,
    )


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


def probe_title_urls(server: "StreamServer", tids: "list[int]") -> "list[str]":
    """Per-title MPEG-TS URLs for several title ids at once (probe.5).

    The probe-stream CLI verifies one recommended title today; the
    fast-mode pipeline will enumerate every episode title's URL
    through this export. Same community-reported /title<N>.ts shape
    as probe_title_url - this module stays the only place that
    knows it.
    """
    return [probe_title_url(server, tid) for tid in tids]


def _atexit_cleanup() -> None:
    """Stop every still-live server at process exit (§7.1 belt-and-suspenders)."""
    for server in list(_LIVE_SERVERS):
        try:
            server.stop()
        except Exception:
            pass


atexit.register(_atexit_cleanup)