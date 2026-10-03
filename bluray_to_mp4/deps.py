"""Dependency checker: first-run guide for Windows (and dev boxes).

Verifies every external tool the app orchestrates and returns plain-
English results with download URLs so the UI can render an actionable
setup screen instead of failing mysteriously mid-rip.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .binaries import find_program
from . import binaries

if TYPE_CHECKING:  # deps stays settings-free at runtime (import-light)
    from .config import Settings

__all__ = ["DependencyCheck", "check_all", "MAKEMKV_URL", "FFMPEG_URL",
           "SUBTITLE_EDIT_URL", "PYTHON_URL", "NVENC_STAMP_KEY"]

MAKEMKV_URL = "https://www.makemkv.com/download/"
FFMPEG_URL = "https://ffmpeg.org/download.html"
SUBTITLE_EDIT_URL = "https://github.com/SubtitleEdit/subtitleedit/releases"
PYTHON_URL = "https://www.python.org/downloads/windows/"

# QW-4 (Bob audit, 2026-10-02): settings.extras key holding the stamp of
# the exact ffmpeg build (path + version line) whose NVENC probe passed.
NVENC_STAMP_KEY = "nvenc_ok_stamp"


@dataclass
class DependencyCheck:
    name: str
    ok: bool
    detail: str
    url: str | None = None
    required: bool = True

    def to_dict(self) -> dict:
        return {
            "name": self.name, "ok": self.ok, "detail": self.detail,
            "url": self.url, "required": self.required,
        }


def _on_path(binary: str) -> str | None:
    return shutil.which(binary)


def check_makemkv() -> DependencyCheck:
    """makemkvcon: PATH, env override, or standard install dirs."""
    from . import binaries
    found = find_program(
        "makemkvcon",
        env_var="B2M_MAKEMKVCON",
        search_dirs=binaries._makemkv_dirs(),
        exe_names=["makemkvcon.exe", "makemkvcon64.exe", "makemkvcon"],
    )
    if not found:
        return DependencyCheck(
            "MakeMKV", False,
            "makemkvcon not found on PATH - install MakeMKV, then re-check",
            MAKEMKV_URL,
        )
    try:
        proc = subprocess.run(
            [found, "-r", "info", "disc:9999"],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        detail = (
            f"makemkvcon did not answer within {exc.timeout}s - another "
            "MakeMKV process is probably still holding the drive. Close "
            "other MakeMKV windows (or end makemkvcon in Task Manager), "
            "then re-check."
            if isinstance(exc, subprocess.TimeoutExpired) else f"makemkvcon failed: {exc}"
        )
        return DependencyCheck("MakeMKV", False, detail, MAKEMKV_URL)
    if proc.returncode != 0 and not proc.stdout:
        return DependencyCheck(
            "MakeMKV", False,
            f"makemkvcon exited with {proc.returncode}", MAKEMKV_URL,
        )
    m = re.search(r"MakeMKV\s+v([\d.]+)", proc.stdout)
    version = m.group(1) if m else "unknown"
    # G8: structured MSG-code match, not substring (a benign file size
    # containing "5055" must not trip this).
    expired = bool(re.search(r"^\s*MSG:(5021|5055),", proc.stdout, re.M)) or (
        "evaluation period has expired" in proc.stdout.lower())
    
    if expired:
        return DependencyCheck(
            "MakeMKV", False,
            f"v{version} found, but the beta key has expired - grab the "
            "current key from the MakeMKV forum and paste it into MakeMKV",
            "https://www.makemkv.com/forum/viewtopic.php?f=5",
        )
    return DependencyCheck(
        "MakeMKV", True, f"makemkvcon v{version} ready", MAKEMKV_URL,
    )


def check_ffmpeg() -> DependencyCheck:
    for binary in ("ffmpeg", "ffmpeg.exe"):
        found = find_program(
            binary,
            env_var="B2M_FFMPEG",
            search_dirs=binaries._ffmpeg_dirs(),
            exe_names=["ffmpeg.exe", "ffmpeg"],
        )
        if found:
            try:
                proc = subprocess.run(
                    [found, "-version"], capture_output=True, text=True,
                    timeout=15, encoding="utf-8", errors="replace",
                )
                first = (proc.stdout or "").splitlines()[0][:80]
                return DependencyCheck("ffmpeg", True, first or "ready", FFMPEG_URL)
            except (OSError, subprocess.TimeoutExpired):
                return DependencyCheck("ffmpeg", False, "found but failed to run", FFMPEG_URL)
    return DependencyCheck("ffmpeg", False, "ffmpeg not found on PATH", FFMPEG_URL)


def _nvenc_cache_stamp(ffmpeg: str) -> str:
    """Identity of the ffmpeg build that would run the NVENC probe.

    Path + the first `-version` line: if either changes (new binary,
    updated build), a cached verdict no longer applies and the real
    hardware probe must run again. The `-version` call is a fast
    metadata read - the expensive part being cached is the NVENC
    encode probe (GPU/driver session) plus its -encoders fallback.
    """
    try:
        proc = subprocess.run(
            [ffmpeg, "-version"], capture_output=True, text=True,
            timeout=15, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return f"{ffmpeg}|"
    lines = (proc.stdout or "").splitlines()
    return f"{ffmpeg}|{lines[0].strip() if lines else ''}"


def _cache_save(settings) -> None:
    """Persist a stamp change; cache is best-effort and must never
    break the Setup screen (read-only disk, locked config, ...)."""
    if settings is None:
        return
    try:
        settings.save()
    except OSError:
        pass


def check_nvenc(settings: "Settings | None" = None) -> DependencyCheck:
    """NVENC capability via a hardware encode probe.

    QW-4 cache: with `settings` (the UI passes its Settings instance),
    a PASSING probe is stamped into settings.extras[NVENC_STAMP_KEY]
    as "<ffmpeg path>|<version line>"; while the exact same build is
    still in place the cached verdict is returned (detail notes
    "cached") and the hardware probe is skipped. A failing probe
    clears the stamp. settings=None (direct/CLI callers) always runs
    the probe. The cache is cosmetic-only by construction: pipeline's
    detect_encoder() re-probes hardware on every real run start, so a
    stale cache can never send an encode down a broken path.
    """
    ffmpeg = find_program(
        "ffmpeg",
        env_var="B2M_FFMPEG",
        search_dirs=binaries._ffmpeg_dirs(),
        exe_names=["ffmpeg.exe", "ffmpeg"],
    )
    if not ffmpeg:
        return DependencyCheck(
            "NVIDIA NVENC", False,
            "skipped: ffmpeg not found (x265 software encode still works)",
            None, required=False,
        )
    extras = getattr(settings, "extras", None)
    # Stamp identity is only needed when there is a cache to consult/
    # update; settings=None (direct callers) skips straight to the probe.
    stamp = _nvenc_cache_stamp(ffmpeg) if extras is not None else ""
    if extras is not None and extras.get(NVENC_STAMP_KEY) == stamp:
        return DependencyCheck(
            "NVIDIA NVENC", True,
            "hardware HEVC encoding available (fast mode, cached probe)",
            None, required=False,
        )
    _nvenc_err = ""
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "nullsrc=s=256x256:d=0.1",
             "-c:v", "hevc_nvenc", "-f", "null", "-"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        proc = None
        _nvenc_err = str(exc)
    else:
        _nvenc_err = ((proc.stderr or "") + (proc.stdout or "")).strip()
    if proc is not None and proc.returncode == 0:
        if extras is not None:
            extras[NVENC_STAMP_KEY] = stamp
            _cache_save(settings)
        return DependencyCheck(
            "NVIDIA NVENC", True,
            "hardware HEVC encoding available (fast mode)", None, required=False,
        )
    # Probe failed: whatever stamp is stored no longer speaks for this
    # build - clear it so the next check re-probes honestly.
    if extras is not None and extras.pop(NVENC_STAMP_KEY, None) is not None:
        _cache_save(settings)
    # Distinguish "this ffmpeg has no NVENC" from "GPU/driver problem":
    # a NVENC-less build errors with 'Unknown encoder' before ever touching
    # the GPU; a driver problem fails differently (or ffmpeg uses a
    # non-CUDA-capable build like the shared one).
    encoders = ""
    try:
        probe = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=30,
        )
        encoders = probe.stdout or ""
    except (OSError, subprocess.TimeoutExpired):
        pass
    if "hevc_nvenc" in encoders:
        detail = "ffmpeg has NVENC support, but the hardware probe failed"
        if _nvenc_err:
            snippet = " ".join(_nvenc_err.split())[:200]
            detail = f"{detail}: {snippet}"
        else:
            detail += " - check GPU drivers (nvidia-smi)"
        detail += "; x265 quality mode still works"
        return DependencyCheck(
            "NVIDIA NVENC", False, detail,
            "https://www.nvidia.com/drivers", required=False,
        )
    return DependencyCheck(
        "NVIDIA NVENC", False,
        "this ffmpeg build lacks NVENC support (software-only build) - "
        "re-download the release zip; x265 quality mode still works",
        None, required=False,
    )


def check_subtitle_edit() -> DependencyCheck:
    """Subtitle Edit headless seconv CLI for PGS/VobSub -> SRT OCR.

    Bundled with the app since v0.2.2 (packaging/tools/SubtitleEdit), so
    the packaged build should always find it next to the exe.
    """
    found = find_program(
        "seconv",
        env_var="B2M_SUBTITLE_EDIT",
        search_dirs=binaries._subtitle_edit_dirs(),
        exe_names=["seconv.exe", "seconv", "SubtitleEdit.exe", "SubtitleEdit"],
    )
    if found:
        return DependencyCheck(
            "Subtitle Edit", True, f"found at {found}", SUBTITLE_EDIT_URL,
            required=False,
        )
    # Common Windows install location fallback.
    for candidate in (
        r"C:\Program Files\Subtitle Edit\SubtitleEdit.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Subtitle Edit\SubtitleEdit.exe"),
    ):
        if os.path.exists(candidate):
            return DependencyCheck(
                "Subtitle Edit", True, f"found at {candidate}", SUBTITLE_EDIT_URL,
                required=False,
            )
    return DependencyCheck(
        "Subtitle Edit", False,
        "not found - subtitle OCR disabled (MP4s still work); reinstall the "
        "latest release zip to get the bundled copy",
        SUBTITLE_EDIT_URL, required=False,
    )


def check_ffprobe() -> DependencyCheck:
    found = find_program(
        "ffprobe",
        env_var="B2M_FFPROBE",
        search_dirs=binaries._ffmpeg_dirs(),
        exe_names=["ffprobe.exe", "ffprobe"],
    )
    if found:
        return DependencyCheck("ffprobe", True, f"found at {found}", FFMPEG_URL)
    return DependencyCheck(
        "ffprobe", False,
        "ffprobe not found (ships with ffmpeg builds)", FFMPEG_URL,
    )


def check_python_encoder_modules() -> DependencyCheck:
    """The UI needs PySide6; CLI-only users can skip it."""
    try:
        import PySide6  # noqa: F401
        return DependencyCheck(
            "PySide6 (UI)", True, f"v{PySide6.__version__} ready",
            None, required=False,
        )
    except ImportError:
        return DependencyCheck(
            "PySide6 (UI)", False,
            "not installed - run: pip install PySide6 (CLI still works)",
            None, required=False,
        )


def check_all(settings: "Settings | None" = None) -> list[DependencyCheck]:
    return [
        check_makemkv(),
        check_ffmpeg(),
        check_ffprobe(),
        check_subtitle_edit(),
        check_nvenc(settings),
        check_python_encoder_modules(),
    ]


def missing_required(checks: list[DependencyCheck]) -> list[DependencyCheck]:
    return [c for c in checks if c.required and not c.ok]