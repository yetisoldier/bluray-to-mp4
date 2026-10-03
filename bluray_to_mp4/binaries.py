"""Windows binary discovery: PATH, env override, then common install dirs.

The MakeMKV (and Subtitle Edit) installers do not add their CLIs to PATH,
so `shutil.which` alone reports "not installed" on healthy machines.
Every wrapper resolves its binary through here.
"""

from __future__ import annotations

import os
import shutil

__all__ = ["find_program", "resolve_makemkvcon", "resolve_ffmpeg",
           "resolve_ffprobe", "resolve_subtitle_edit",
           "resolve_realesrgan_ncnn"]

def _program_files() -> str:
    return os.environ.get("ProgramFiles", r"C:\Program Files")


def _program_files_x86() -> str:
    return os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")


def _local_appdata() -> str:
    return os.environ.get("LOCALAPPDATA", "")


def _app_internal_dirs() -> list[str]:
    """Directories inside the packaged app where bundled tools may live.

    In the packaged app the bundled tools take priority over anything on
    PATH: we ship the right ffmpeg (with NVENC) and SubtitleEdit, and a
    random system ffmpeg must never shadow them. Unpackaged (dev) runs
    keep PATH first so a dev's own tools still work.
    """
    import sys
    internal = []
    if getattr(sys, "frozen", False):  # PyInstaller onefile/onedir
        roots = [
            os.path.join(sys._MEIPASS, "tools"),
            os.path.join(sys._MEIPASS, "..", "tools"),
            os.path.join(sys.executable, "..", "tools"),
        ]
        internal.extend(roots)
        # Subtitle Edit ships as a subfolder of the bundled tools dir;
        # listed after the roots so our own ffmpeg.exe always wins over
        # anything the SE folder might contain.
        internal.extend(os.path.join(r, "SubtitleEdit") for r in roots)
    return internal


def _search_dirs(extra: list[str]) -> list[str]:
    """Bundled-app tools first (packaged builds), then caller dirs."""
    return _app_internal_dirs() + extra


def _makemkv_dirs() -> list[str]:
    return [
        os.path.join(_program_files(), "MakeMKV"),
        os.path.join(_program_files_x86(), "MakeMKV"),
        os.path.join(_local_appdata(), "Programs", "MakeMKV"),
    ]


def _ffmpeg_dirs() -> list[str]:
    return [
        os.path.join(_local_appdata(), "Microsoft", "WinGet", "Links"),
        r"C:\ffmpeg\bin",
        os.path.join(_program_files(), "ffmpeg", "bin"),
        os.path.join(_program_files(), "FFmpeg", "bin"),
    ]


def _subtitle_edit_dirs() -> list[str]:
    return [
        # Bundled SubtitleEdit ships inside the app zip (packaging/tools/)
        # as tools/SubtitleEdit/SubtitleEdit.exe; _app_internal_dirs covers
        # it automatically in packaged builds.
        "SubtitleEdit",
        os.path.join(_program_files(), "Subtitle Edit"),
        os.path.join(_local_appdata(), "Programs", "Subtitle Edit"),
    ]


def _realesrgan_dirs() -> list[str]:
    home = os.path.expanduser("~")
    local_root = os.path.join(
        home,
        ".local",
        "share",
        "bluray-to-mp4",
        "tools",
        "realesrgan-ncnn-vulkan-v0.2.0-ubuntu",
        "realesrgan-ncnn-vulkan-v0.2.0-ubuntu",
    )
    return [
        local_root,
        "RealESRGAN",
        "realesrgan-ncnn-vulkan",
        os.path.join(_program_files(), "RealESRGAN"),
        os.path.join(_program_files(), "Real-ESRGAN"),
        os.path.join(_local_appdata(), "Programs", "RealESRGAN"),
        os.path.join(_local_appdata(), "Programs", "Real-ESRGAN"),
    ]


def find_program(
    name: str,
    *,
    env_var: str = "",
    search_dirs: list[str] | None = None,
    exe_names: list[str] | None = None,
) -> str | None:
    """Resolve a program to a full path, or None if not found.

    Order: environment override (path or name), PATH, bundled tools inside
    the packaged app, then each search directory trying each candidate
    filename.
    """
    if env_var:
        override = os.environ.get(env_var, "").strip()
        if override:
            if os.path.isfile(override):
                return override
            on_path = shutil.which(override)
            if on_path:
                return on_path
    bundled = _app_internal_dirs()
    if bundled:
        # Packaged app: our bundled ffmpeg (with NVENC) and SubtitleEdit
        # win over any random system copy found on PATH.
        for directory in bundled:
            for candidate in exe_names or [name]:
                full = os.path.join(directory, candidate)
                if os.path.isfile(full):
                    return full
    on_path = shutil.which(name)
    if on_path:
        return on_path
    for directory in (search_dirs or []):
        for candidate in exe_names or [name]:
            full = os.path.join(directory, candidate)
            if os.path.isfile(full):
                return full
    return None


def resolve_makemkvcon(binary: str = "makemkvcon") -> str:
    """makemkvcon path: explicit binary, env override, PATH, or install dirs.

    Returns the given name unchanged when nothing is found, so callers
    raise their typed, hint-rich error instead of a raw FileNotFoundError.
    """
    if binary != "makemkvcon" and (os.path.isfile(binary) or shutil.which(binary)):
        return binary
    found = find_program(
        "makemkvcon",
        env_var="B2M_MAKEMKVCON",
        search_dirs=_makemkv_dirs(),
        exe_names=["makemkvcon.exe", "makemkvcon64.exe", "makemkvcon"],
    )
    return found or binary


def resolve_ffmpeg(binary: str = "ffmpeg") -> str:
    if binary != "ffmpeg" and (os.path.isfile(binary) or shutil.which(binary)):
        return binary
    found = find_program(
        "ffmpeg",
        env_var="B2M_FFMPEG",
        search_dirs=_ffmpeg_dirs(),
        exe_names=["ffmpeg.exe", "ffmpeg"],
    )
    return found or binary


def resolve_ffprobe(binary: str = "ffprobe") -> str:
    if binary != "ffprobe" and (os.path.isfile(binary) or shutil.which(binary)):
        return binary
    found = find_program(
        "ffprobe",
        env_var="B2M_FFPROBE",
        search_dirs=_ffmpeg_dirs(),
        exe_names=["ffprobe.exe", "ffprobe"],
    )
    return found or binary


def resolve_subtitle_edit(binary: str = "seconv") -> str:
    if binary not in ("SubtitleEdit", "SubtitleEdit.exe", "seconv", "seconv.exe") and (
        os.path.isfile(binary) or shutil.which(binary)
    ):
        return binary
    found = find_program(
        binary,
        env_var="B2M_SUBTITLE_EDIT",
        search_dirs=_subtitle_edit_dirs(),
        exe_names=["seconv.exe", "seconv", "SubtitleEdit.exe", "SubtitleEdit"],
    )
    return found or binary


def resolve_realesrgan_ncnn(binary: str = "realesrgan-ncnn-vulkan") -> str:
    if binary != "realesrgan-ncnn-vulkan" and (
        os.path.isfile(binary) or shutil.which(binary)
    ):
        return binary
    found = find_program(
        "realesrgan-ncnn-vulkan",
        env_var="B2M_REALESRGAN_NCNN",
        search_dirs=_realesrgan_dirs(),
        exe_names=["realesrgan-ncnn-vulkan.exe", "realesrgan-ncnn-vulkan"],
    )
    return found or binary
