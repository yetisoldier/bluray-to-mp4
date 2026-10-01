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

from .binaries import find_program
from . import binaries

__all__ = ["DependencyCheck", "check_all", "MAKEMKV_URL", "FFMPEG_URL",
           "SUBTITLE_EDIT_URL", "PYTHON_URL"]

MAKEMKV_URL = "https://www.makemkv.com/download/"
FFMPEG_URL = "https://ffmpeg.org/download.html"
SUBTITLE_EDIT_URL = "https://github.com/SubtitleEdit/subtitleedit/releases"
PYTHON_URL = "https://www.python.org/downloads/windows/"


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
        return DependencyCheck("MakeMKV", False, f"makemkvcon failed: {exc}", MAKEMKV_URL)
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


def check_nvenc() -> DependencyCheck:
    """NVENC capability via a hardware encode probe."""
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
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "nullsrc=s=256x256:d=0.1",
             "-c:v", "hevc_nvenc", "-f", "null", "-"],
            capture_output=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        proc = None
    if proc is not None and proc.returncode == 0:
        return DependencyCheck(
            "NVIDIA NVENC", True,
            "hardware HEVC encoding available (fast mode)", None, required=False,
        )
    return DependencyCheck(
        "NVIDIA NVENC", False,
        "not detected - x265 quality mode still works, just slower",
        "https://www.nvidia.com/drivers", required=False,
    )


def check_subtitle_edit() -> DependencyCheck:
    """Subtitle Edit CLI (SubtitleEdit.exe) for PGS->SRT OCR."""
    found = find_program(
        "SubtitleEdit",
        env_var="B2M_SUBTITLE_EDIT",
        search_dirs=binaries._subtitle_edit_dirs(),
        exe_names=["SubtitleEdit.exe", "SubtitleEdit"],
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
        "not found - subtitle OCR disabled until installed (MP4s still work)",
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


def check_all() -> list[DependencyCheck]:
    return [
        check_makemkv(),
        check_ffmpeg(),
        check_ffprobe(),
        check_subtitle_edit(),
        check_nvenc(),
        check_python_encoder_modules(),
    ]


def missing_required(checks: list[DependencyCheck]) -> list[DependencyCheck]:
    return [c for c in checks if c.required and not c.ok]