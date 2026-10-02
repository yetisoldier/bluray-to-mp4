"""MakeMKV robot-mode CLI wrapper and output parser.

Wire formats verified against MakeMKV 1.17.x robot output as used in
production by Automatic Ripping Machine (arm/ripper/makemkv.py) and the
community notes in arm_wiki/MakeMKV-Codes.md:

  MSG:<code>,<flags>,<count>,"<formatted>","<sprintf>",<params...>
  DRV:<index>,<status>,999,<disc-type>,"<drive name>","<disc label>","<path>"
  TCOUNT:<count>
  CINFO:<id>,<code>,"<value>"
  TINFO:<tid>,<id>,<code>,"<value>"
  SINFO:<tid>,<sid>,<id>,<code>,"<value>"
  PRGV:<current>,<total>,<max>
  PRGC/PRGT:<code>,<id>,"<name>"

Everything here is dependency-free (standard library only) so the parser
is fully testable on any machine; makemkvcon is only required at runtime.
"""

from __future__ import annotations

import csv
import io
import subprocess
from dataclasses import dataclass
from typing import Callable, Iterator

from .binaries import resolve_makemkvcon
from .models import Attribute, Disc, StreamInfo, Title

__all__ = [
    "MakeMkvError",
    "BetaKeyExpiredError",
    "DiscOpenError",
    "TitleRipError",
    "DriveEntry",
    "EncodeCancelled",
    "parse_line",
    "find_drives",
    "scan_disc",
    "rip_titles",
    "make_runner",
]

Runner = Callable[[str, list[str]], Iterator[str]]

# Well-known MakeMKV message codes (see MessageID in ARM's makemkv.py).
BETA_EXPIRED_CODES = {5021, 5055}
DISC_OPEN_ERROR = 5010
TITLE_RIP_ERROR = 5003
RIP_COMPLETED_CODES = {5004, 5037}
FILE_ADDED = 3307

# DRV disc-type field values (arm_wiki/MakeMKV-Codes.md).
DISC_TYPE_CODES = {0: None, 1: "dvd", 12: "bluray", 28: "bluray"}

_BETA_HINT = (
    "MakeMKV's evaluation period has expired. Grab the current beta key from "
    "the MakeMKV forum (https://www.makemkv.com/forum/viewtopic.php?f=5) "
    "or purchase a lifetime license."
)


class MakeMkvError(RuntimeError):
    """Base class for MakeMKV failures."""


class BetaKeyExpiredError(MakeMkvError):
    """MakeMKV beta key expired or version too old (MSG 5021/5055)."""


class DiscOpenError(MakeMkvError):
    """MakeMKV could not open the disc (MSG 5010)."""


class TitleRipError(MakeMkvError):
    """One or more titles failed to rip (MSG 5003 / zero saved)."""


class EncodeCancelled(RuntimeError):
    """Run cancelled by the user (rip or encode path).

    run_pipeline attaches the partial PipelineResult to .result before
    re-raising, so jobs completed before the cancel keep completed=True.
    """

    result = None


def _fields(payload: str) -> list[str]:
    """CSV-split a robot-mode payload, honoring quoted commas."""
    return next(csv.reader(io.StringIO(payload), skipinitialspace=True))


def parse_line(line: str):
    """Parse one robot-mode line into a (kind, payload) tuple."""
    kind, _, payload = line.partition(":")
    kind = kind.strip()
    if kind == "MSG":
        f = _fields(payload)
        code, flags, count = int(f[0]), int(f[1]), int(f[2])
        text = f[3] if len(f) > 3 else ""
        params = f[5:] if len(f) > 5 else []
        return "MSG", (code, flags, count, text, params)
    if kind == "DRV":
        f = _fields(payload)
        while len(f) < 7:
            f.append("")
        return "DRV", (int(f[0]), int(f[1]), int(f[3]), f[4], f[5], f[6])
    if kind == "TCOUNT":
        return "TCOUNT", (int(_fields(payload)[0]),)
    if kind == "PRGV":
        f = _fields(payload)
        return "PRGV", tuple(int(x) for x in f[:3])
    if kind in ("PRGC", "PRGT"):
        f = _fields(payload)
        return kind, (int(f[0]), int(f[1]), f[2] if len(f) > 2 else "")
    if kind in ("CINFO", "TINFO", "SINFO"):
        f = _fields(payload)
        if kind == "CINFO":
            tid = sid = None
            id_, code = int(f[0]), int(f[1])
            value = f[2] if len(f) > 2 else ""
        elif kind == "TINFO":
            tid, sid = int(f[0]), None
            id_, code = int(f[1]), int(f[2])
            value = f[3] if len(f) > 3 else ""
        else:
            tid, sid = int(f[0]), int(f[1])
            id_, code = int(f[2]), int(f[3])
            value = f[4] if len(f) > 4 else ""
        return kind, (tid, sid, id_, code, value)
    return kind.upper(), payload


@dataclass
class DriveEntry:
    """One optical drive from a `makemkvcon -r info disc:9999` scan."""

    index: int
    status: int  # 0 empty, 1 open, 2 ready, 3 loading, 256 not attached
    disc_type: str | None  # 'dvd' | 'bluray' | None
    drive_name: str
    disc_label: str
    path: str

    @property
    def has_disc(self) -> bool:
        return self.status == 2 and self.disc_type is not None


class _CancelState:
    """Shared handle between a runner and its caller: cancel kills the child."""

    def __init__(self):
        self.requested = False
        self.proc: subprocess.Popen | None = None

    def cancel(self) -> bool:
        self.requested = True
        return True


def make_runner(timeout: float | None = None, cancel_state: _CancelState | None = None) -> Runner:
    """Build the default subprocess runner that streams makemkvcon stdout.

    cancel_state, when given, is linked to the spawned Popen so a cancel
    request can terminate makemkvcon mid-rip (GeneratorExit propagates).
    """

    def run(binary: str, args: list[str]) -> Iterator[str]:
        binary = resolve_makemkvcon(binary)
        try:
            proc = subprocess.Popen(
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
        if cancel_state is not None:
            cancel_state.proc = proc
        assert proc.stdout is not None
        try:
            for raw in proc.stdout:
                if cancel_state is not None and cancel_state.requested:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    raise EncodeCancelled("rip cancelled by user")
                yield raw.rstrip("\r\n")
        finally:
            proc.stdout.close()
            # GeneratorExit lands here when the caller stops consuming
            # lines mid-rip (cancel path): terminate instead of waiting
            # out a multi-hour rip.
            if cancel_state is not None and cancel_state.requested:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            else:
                rc = proc.wait(timeout=timeout)
                if rc != 0:
                    raise MakeMkvError(f"makemkvcon exited with code {rc}")

    return run


def find_drives(
    binary: str = "makemkvcon", *, runner: Runner | None = None, timeout: float | None = None
) -> list[DriveEntry]:
    """List attached optical drives and any loaded disc labels."""
    run = runner or make_runner(timeout)
    drives: list[DriveEntry] = []
    for line in run(binary, ["-r", "info", "disc:9999"]):
        kind, payload = parse_line(line)
        if kind == "DRV":
            idx, status, dtype_code, name, label, path = payload
            if status != 256:  # 256 = slot not attached
                drives.append(
                    DriveEntry(idx, status, DISC_TYPE_CODES.get(dtype_code), name, label, path)
                )
    return drives


def scan_disc(
    index: int,
    binary: str = "makemkvcon",
    *,
    minlength: int = 120,
    runner: Runner | None = None,
    timeout: float | None = None,
    on_line: "Callable[[str, str], None] | None" = None,
) -> Disc:
    """Scan a disc and build the full Disc model (titles, streams, languages).

    on_line(kind, text), when given, receives a human-readable progress
    line for notable events (MSG codes, titles seen) so the UI can show
    the scan is alive instead of appearing frozen.
    """
    run = runner or make_runner(timeout)
    disc = Disc(index=index)
    title: Title | None = None
    stream: StreamInfo | None = None

    def _report(text: str) -> None:
        if on_line is not None:
            on_line("scan", text)

    for line in run(
        binary,
        ["-r", "--cache=1", "info", f"disc:{index}", f"--minlength={minlength}"],
    ):
        kind, payload = parse_line(line)
        if on_line is not None:
            if kind == "MSG":
                _text = payload[3] if len(payload) > 3 else ""
                if _text:
                    _report(_text)
            elif kind == "TINFO":
                _report(f"Reading title {payload[0]} ...")
        if kind == "MSG":
            code = payload[0]
            if code in BETA_EXPIRED_CODES:
                raise BetaKeyExpiredError(_BETA_HINT)
            if code == DISC_OPEN_ERROR:
                raise DiscOpenError(
                    "MakeMKV could not open the disc. Check the drive, disc condition, "
                    "and that the disc is a video Blu-ray/DVD."
                )
        elif kind == "DRV":
            idx, status, dtype_code, name, label, _path = payload
            if idx == index:
                disc.drive_name = name
                disc.disc_label = label
                disc.media_type = DISC_TYPE_CODES.get(dtype_code) or ""
        elif kind == "CINFO":
            _tid, _sid, id_, code, value = payload
            disc.attrs[id_] = Attribute(id_, code, value)
        elif kind == "TINFO":
            tid, _sid, id_, code, value = payload
            if title is None or title.tid != tid:
                title = Title(tid=tid)
                disc.titles.append(title)
                stream = None
            title.attrs[id_] = Attribute(id_, code, value)
        elif kind == "SINFO":
            tid, sid, id_, code, value = payload
            if title is None or title.tid != tid:
                title = Title(tid=tid)
                disc.titles.append(title)
            if stream is None or stream.tid != tid or stream.sid != sid:
                stream = StreamInfo(tid=tid, sid=sid)
                title.streams.append(stream)
            stream.attrs[id_] = Attribute(id_, code, value)
    return disc


def rip_titles(
    index: int,
    tids: list[int],
    outdir: str,
    *,
    minlength: int = 120,
    binary: str = "makemkvcon",
    runner: Runner | None = None,
    on_progress: Callable[[float, tuple], None] | None = None,
    on_info: Callable[[str], None] | None = None,
    timeout: float | None = None,
    cancel: Callable[[], bool] | None = None,
) -> list[str]:
    """Rip the given title ids to MKV files in outdir; return created paths.

    `on_progress(fraction, prgv)` fires on every PRGV line (fraction is
    current/max from MakeMKV's constant 65536 scale). `on_info(text)` gets
    each MSG line's formatted text so callers can show live status during
    the long BD+ open phase, when PRGV legitimately sits at 0. cancel()
    -> True terminates makemkvcon (default runner) or stops consuming
    output mid-rip and raises EncodeCancelled.
    """
    run = runner or make_runner(timeout)
    args = [
        "-r",
        "mkv",
        f"disc:{index}",
        ",".join(str(t) for t in tids),
        str(outdir),
        f"--minlength={minlength}",
        # Without --progress, robot mode emits NO PRGV progress lines to
        # stdout and the whole rip phase looks frozen at 0% (found in first
        # real-disc validation, 2026-10-02). '-' = stdout.
        "--progress=-",
    ]
    saved: list[str] = []
    failures: list[str] = []
    for line in run(binary, args):
        if cancel is not None and cancel():
            raise EncodeCancelled("rip cancelled by user")
        kind, payload = parse_line(line)
        if kind == "MSG":
            code, _flags, _count, text, params = payload
            if on_info is not None and text:
                on_info(text)
            if code in BETA_EXPIRED_CODES:
                raise BetaKeyExpiredError(_BETA_HINT)
            if code == DISC_OPEN_ERROR:
                raise DiscOpenError("MakeMKV could not open the disc.")
            if code == TITLE_RIP_ERROR:
                failures.append(text)
            elif code == FILE_ADDED and params:
                saved.append(params[0])
            elif code in RIP_COMPLETED_CODES and params:
                if params[0].isdigit() and int(params[0]) == 0 and not failures:
                    raise MakeMkvError("MakeMKV finished but saved 0 titles.")
        elif kind == "PRGV" and on_progress is not None:
            current, _total, maximum = payload
            on_progress(current / maximum if maximum else 0.0, payload)
    if not saved:
        if failures:
            raise TitleRipError(failures[0])
        raise MakeMkvError("makemkvcon finished without producing any files.")
    return saved