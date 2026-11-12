"""Final-output container seam (v0.2.38).

Every container fact about the FINISHED video file lives here and only
here. MKV is the sole produced container (Eric 2026-11-11: no toggle,
no sidecar MP4). The retired MP4 profile - extension ".mp4", muxer
"mp4", text codec "mov_text", "-movflags +faststart" - returns, if
ever, as a second named profile in THIS module. It is never
rediscovered by grepping literals.
"""
from __future__ import annotations

VIDEO_EXT = ".mkv"
FFMPEG_FORMAT = "matroska"
TEXT_SUBTITLE_CODEC = "subrip"

# Bitmap subtitle codecs that may be STREAM-COPIED into Matroska
# (never re-encoded - ffmpeg ships no PGS/VobSub encoder). These are
# the only two bitmap codecs a MakeMKV rip of a Blu-ray/DVD carries.
BITMAP_COPY_CODECS = frozenset({"hdmv_pgs_subtitle", "dvd_subtitle"})
