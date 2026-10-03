"""Built-in monitoring API: LAN-visible status for remote check-ins.

A dependency-free (stdlib http.server) read-only status service that runs
inside the app (and optionally beside CLI runs). The same store is fed by
pipeline callbacks and the UI, so any run is monitorable remotely:

    GET /health   -> {"ok": true, "busy": ...}
    GET /status   -> full live state: current stage, per-title progress,
                     ETA, warnings, last completed outputs
    GET /history  -> recent completed jobs (newest first)

Requests must carry the configured token as a Bearer header or ?token=
query parameter unless the token is empty (local-only default). The
server binds 127.0.0.1 by default; point it at 0.0.0.0 for LAN use.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__all__ = [
    "JobRecord",
    "MonitorState",
    "MonitorServer",
    "state",
    "start_server",
]

_history_lock = threading.Lock()


@dataclass
class JobRecord:
    """One completed output (kept for /history)."""

    name: str
    kind: str  # 'movie' | 'series'
    duration_seconds: int
    encoder: str
    srt: bool = False
    # QW-6 (Bob audit, 2026-10-02): output size at job_done time so
    # History can show size + achieved GB/hour - the number Eric
    # validates by hand every run. 0 = unknown/missing.
    size_bytes: int = 0
    # v0.2.18: the run's size target (Review-page spin); 0.0 = a
    # pre-slider record, rendered as an empty History cell.
    target_gb_per_hour: float = 0.0
    warnings: list[str] = field(default_factory=list)
    finished_at: float = field(default_factory=time.time)

    @property
    def gb_per_hour(self) -> float:
        """Achieved size rate in GB (decimal) per hour of video.

        Matches the project's size-budget units (encode.py: GB = 1e9,
        2.0 GB/hr Blu-ray, 1.0 GB/hr DVD), so the History column reads
        directly against the budget. 0.0 when duration is unknown.
        """
        if self.duration_seconds > 0:
            return self.size_bytes / 1e9 / (self.duration_seconds / 3600)
        return 0.0


class MonitorState:
    """Thread-safe shared status store fed by pipeline callbacks."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.busy = False
        self.current_job: str | None = None
        self.stage: str | None = None
        self.jobs_total = 0
        self.jobs_done = 0
        self.progress = 0.0  # 0..1 for the active stage
        self.eta_seconds: float | None = None
        self.disc_name: str | None = None
        self.disc_type: str | None = None
        self.series_mode: bool | None = None
        self.encoder: str | None = None
        self.warnings: list[str] = []
        self.recent: list[JobRecord] = []
        self.started_at: float | None = None
        self.updated_at: float = time.time()
        self.last_error: str | None = None

    def begin(self, disc_name: str, disc_type: str, *, series_mode: bool,
              encoder: str, jobs_total: int) -> None:
        with self._lock:
            self.busy = True
            self.disc_name = disc_name
            self.disc_type = disc_type
            self.series_mode = series_mode
            self.encoder = encoder
            self.jobs_total = jobs_total
            self.jobs_done = 0
            self.current_job = None
            self.stage = "starting"
            self.progress = 0.0
            self.eta_seconds = None
            self.warnings = []
            self.started_at = time.time()
            self.updated_at = time.time()
            self.last_error = None

    def update(self, *, stage: str | None = None, current_job: str | None = None,
               progress: float | None = None, eta_seconds: float | None = None,
               warning: str | None = None) -> None:
        with self._lock:
            if stage is not None:
                self.stage = stage
            if current_job is not None:
                self.current_job = current_job
            if progress is not None:
                self.progress = max(0.0, min(progress, 1.0))
            if eta_seconds is not None:
                self.eta_seconds = eta_seconds
            if warning is not None:
                self.warnings.append(warning)
            self.updated_at = time.time()

    def job_done(self, record: JobRecord) -> None:
        with self._lock:
            self.jobs_done += 1
            with _history_lock:
                self.recent.insert(0, record)
                del self.recent[50:]  # keep the newest 50
            self.updated_at = time.time()

    def fail(self, message: str) -> None:
        with self._lock:
            self.busy = False
            self.stage = "error"
            self.last_error = message
            self.updated_at = time.time()

    def finish(self) -> None:
        with self._lock:
            self.busy = False
            self.stage = "idle"
            self.progress = 0.0
            self.eta_seconds = None
            self.updated_at = time.time()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "busy": self.busy,
                "disc": self.disc_name,
                "disc_type": self.disc_type,
                "series_mode": self.series_mode,
                "encoder": self.encoder,
                "stage": self.stage,
                "current_job": self.current_job,
                "jobs_done": self.jobs_done,
                "jobs_total": self.jobs_total,
                "progress": round(self.progress, 4),
                "eta_seconds": self.eta_seconds,
                "warnings": list(self.warnings),
                "started_at": self.started_at,
                "updated_at": self.updated_at,
                "last_error": self.last_error,
            }

    def history(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return [
                {
                    "name": r.name,
                    "kind": r.kind,
                    "duration_seconds": r.duration_seconds,
                    "encoder": r.encoder,
                    "srt": r.srt,
                    "size_bytes": r.size_bytes,
                    "target_gb_per_hour": r.target_gb_per_hour,
                    "gb_per_hour": round(r.gb_per_hour, 2),
                    "warnings": list(r.warnings),
                    "finished_at": r.finished_at,
                }
                for r in self.recent[:limit]
            ]


state = MonitorState()


class _Handler(BaseHTTPRequestHandler):
    server_version = "b2m-mon/1.0"

    def _send(self, code: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        token = getattr(self.server, "token", "")
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {token}":
            return True
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(self.path).query)
        return q.get("token", [""])[0] == token

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("", "/health"):
            self._send(200, {"ok": True, **self.server.state.snapshot()})  # type: ignore[attr-defined]
            return
        if not self._authorized():
            self._send(401, {"error": "missing or invalid token"})
            return
        if path == "/status":
            self._send(200, self.server.state.snapshot())  # type: ignore[attr-defined]
        elif path == "/history":
            self._send(200, {"jobs": self.server.state.history()})  # type: ignore[attr-defined]
        else:
            self._send(404, {"error": "unknown endpoint",
                             "endpoints": ["/health", "/status", "/history"]})

    def log_message(self, *args) -> None:  # keep the console quiet
        pass


class MonitorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], monitor_state: MonitorState,
                 token: str = ""):
        super().__init__(addr, _Handler)
        self.state = monitor_state
        self.token = token


def lan_ip() -> str:
    """Best-effort LAN IPv4 address (no packet is actually sent)."""
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))  # external route hint; nothing sent
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return "127.0.0.1"


active_server: MonitorServer | None = None
"""The running monitor instance, if any (owned by start/stop_server)."""


def start_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str = "",
    monitor_state: MonitorState | None = None,
) -> tuple[MonitorServer, str]:
    """Start the monitor in a background thread; returns (server, base_url).

    The server is read-only: it can never trigger rips, encodes, or
    configuration changes. Bind host 0.0.0.0 and set a token for LAN
    monitoring from another machine. Idempotent: if a server is already
    running (started through this helper), it is returned as-is so UI
    toggles can't stack duplicate listeners.
    """
    global active_server
    if active_server is not None:
        host, port = active_server.server_address
        base = f"http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}"
        return active_server, base
    monitor_state = monitor_state or state
    server = MonitorServer((host, port), monitor_state, token)
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="b2m-monitor")
    thread.start()
    base = f"http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}"
    active_server = server
    return server, base


def stop_server() -> bool:
    """Stop the running monitor (the UI toggle's 'off' path)."""
    global active_server
    if active_server is None:
        return False
    server, active_server = active_server, None
    server.shutdown()
    server.server_close()
    return True