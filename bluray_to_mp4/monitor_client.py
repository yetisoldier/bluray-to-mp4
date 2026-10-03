"""LAN monitor watcher for bluray-to-mp4.

Uses only the Python standard library so OpenClaw, cron, and the packaged app
can poll the same monitor endpoint without depending on a JavaScript runtime.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urljoin

from .monitor import classify_snapshot

__all__ = ["WatchResult", "check_monitor", "format_watch_result"]


@dataclass(frozen=True)
class WatchResult:
    reachable: bool
    status: str
    message: str
    snapshot: dict | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.reachable and self.status in {"idle", "running"}


def _status_url(base_url: str) -> str:
    base = base_url.rstrip("/") + "/"
    return urljoin(base, "status")


def _fetch_json(url: str, *, token: str = "", timeout: float = 10.0) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_monitor(
    base_url: str,
    *,
    token: str = "",
    timeout: float = 10.0,
    stale_seconds: float = 300.0,
    now: float | None = None,
) -> WatchResult:
    """Poll a monitor URL once and classify the result."""
    try:
        snapshot = _fetch_json(_status_url(base_url), token=token, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return WatchResult(
            False, "unreachable", f"HTTP {exc.code}: {exc.reason}", error=str(exc)
        )
    except (OSError, TimeoutError, json.JSONDecodeError) as exc:
        return WatchResult(False, "unreachable", str(exc), error=str(exc))

    health = classify_snapshot(
        snapshot, now=now or time.time(), stale_seconds=stale_seconds
    )
    status = str(health.get("status") or "unknown")
    reasons = [str(r) for r in health.get("reasons") or [] if r]
    job = snapshot.get("current_job") or snapshot.get("disc") or "no active job"
    stage = snapshot.get("stage") or "unknown stage"
    progress = float(snapshot.get("progress") or 0.0) * 100.0
    if reasons:
        message = (
            f"{status.upper()}: {job} - {stage} at {progress:.1f}% "
            f"({'; '.join(reasons)})"
        )
    else:
        message = f"{status.upper()}: {job} - {stage} at {progress:.1f}%"
    return WatchResult(True, status, message, snapshot=snapshot)


def format_watch_result(result: WatchResult, *, as_json: bool = False) -> str:
    if as_json:
        return json.dumps({
            "reachable": result.reachable,
            "ok": result.ok,
            "status": result.status,
            "message": result.message,
            "error": result.error,
            "snapshot": result.snapshot,
        }, indent=2)
    if result.reachable:
        return result.message
    return f"UNREACHABLE: {result.message}"
