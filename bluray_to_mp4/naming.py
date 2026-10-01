"""Output filename sanitizing and construction (Windows-safe)."""

from __future__ import annotations

import re

__all__ = ["sanitize_filename", "movie_output_name", "subtitle_sidecar"]

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(name: str, *, maxlen: int = 150) -> str:
    """Return a Windows-legal filename fragment."""
    name = _ILLEGAL.sub("", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        return "Untitled"
    if name.upper() in _WINDOWS_RESERVED:
        name = f"_{name}"
    if len(name) > maxlen:
        name = name[:maxlen].rstrip(" .")
    return name


def movie_output_name(title: str, *, year: int | None = None) -> str:
    """'Movie Title (2019).mp4' - Plex/Jellyfin movie naming standard."""
    base = sanitize_filename(title)
    if year:
        return f"{base} ({year}).mp4"
    return f"{base}.mp4"


def subtitle_sidecar(video_path: str) -> str:
    """'Show - S01E01.mp4' -> 'Show - S01E01.srt'."""
    return re.sub(r"\.mp4$", ".srt", video_path, flags=re.I)