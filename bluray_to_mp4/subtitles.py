"""Subtitle extraction and OCR: PGS/VOBsub from ripped MKV to sidecar SRT.

Blu-ray subtitles are image-based (PGS) and MP4 cannot contain them, so
the recommended track is extracted to .sup and OCR'd with Subtitle
Edit's headless command-line converter, seconv (Tesseract engine). DVDs carry VobSub,
which extracts to a single-track MKV instead (v0.2.16: ffmpeg has no
vobsub muxer; matroska is the only container that accepts a
dvd_subtitle stream copy). The SRT output is then parsed and
normalized locally; a zero-cue result is treated as an OCR failure and
never silently shipped. The normalized SRT is kept as an editable
sidecar next to the MP4 and embedded into the MP4 as a soft mov_text
track (v0.2.16).
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

from .binaries import resolve_ffmpeg, resolve_subtitle_edit
from .srt import Cue, SrtError, normalize_cues, parse_srt, write_srt_file

__all__ = [
    "SubtitleOcrError",
    "OcrResult",
    "SubtitleEditOcr",
    "extract_subtitles",
    "run_subtitle_ocr",
    "mux_language",
    "mux_srt_into_mp4",
]

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


class SubtitleOcrError(RuntimeError):
    """Subtitle extraction or OCR failed."""


@dataclass
class OcrResult:
    srt_path: str
    cue_count: int
    notes: list[str] = field(default_factory=list)


def extract_subtitles(
    mkv_path: str,
    stream_index: int,
    out_path: str,
    *,
    binary: str = "ffmpeg",
) -> str:
    """Extract one subtitle stream (PGS/VOBsub) losslessly to its own file."""
    binary = resolve_ffmpeg(binary)
    try:
        result = subprocess.run(
            [
                binary, "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
                "-i", mkv_path, "-map", f"0:{stream_index}", "-c", "copy", out_path,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise SubtitleOcrError(
            f"ffmpeg not found for extraction: {exc}. Install ffmpeg from "
            "https://ffmpeg.org/download.html and retry."
        ) from exc
    if result.returncode != 0 or not os.path.exists(out_path):
        raise SubtitleOcrError(
            f"subtitle extraction failed: {result.stderr.strip()[:300]}"
        )
    return out_path


@dataclass
class SubtitleEditOcr:
    """OCR via Subtitle Edit's headless seconv converter (Tesseract)."""

    binary: str = "seconv"
    ocr_engine: str = "tesseract"
    language: str = "en"

    def command(self, in_path: str, out_dir: str) -> list[str]:
        """seconv CLI: /convert <pattern> <format> /ocrengine:<engine>.

        The converter writes <basename>.srt next to the input's name inside
        out_dir; we move/verify afterwards.
        """
        return [
            self.binary,
            "/convert", in_path,
            "subrip",
            f"/ocrengine:{self.ocr_engine}",
            f"/ocrlanguage:{self.language}",
            "/outputfolder:" + out_dir,
        ]

    def run(self, in_path: str, out_dir: str, *, runner: Runner | None = None) -> str:
        """Run the conversion and return the produced .srt path."""
        cmd = self.command(in_path, out_dir)
        self.binary = resolve_subtitle_edit(self.binary)
        cmd[0] = self.binary
        if runner is None:
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                )
            except FileNotFoundError as exc:
                raise SubtitleOcrError(
                    "Subtitle Edit seconv not found on PATH. Install Subtitle Edit from "
                    "https://github.com/SubtitleEdit/subtitleedit/releases "
                    "with seconv.exe (and Tesseract via the UB Mannheim build), then retry. "
                    f"Details: {exc}"
                ) from exc
            if proc.returncode != 0:
                raise SubtitleOcrError(
                    f"Subtitle Edit seconv exited with code {proc.returncode}: "
                    f"{proc.stderr.strip()[:300]}"
                )
        else:
            proc = runner(cmd)
            if proc.returncode != 0:
                raise SubtitleOcrError(
                    f"Subtitle Edit seconv exited with code {proc.returncode}"
                )

        expected = os.path.splitext(os.path.basename(in_path))[0] + ".srt"
        produced = os.path.join(out_dir, expected)
        if not os.path.exists(produced):
            raise SubtitleOcrError(
                f"Subtitle Edit seconv produced no SRT (expected {produced}). "
                "Check the Tesseract installation."
            )
        return produced


def _intermediate_ext(codec_name: str) -> str:
    """Container extension for the extracted subtitle track.

    PGS (hdmv_pgs_subtitle) -> .sup, DVD VobSub (dvd_subtitle) -> .mkv
    (ffmpeg's only muxer that accepts dvd_subtitle copy; Subtitle Edit
    reads VobSub from MKV directly). Unknown subtitle codecs also get
    .mkv: MKV is the most permissive container, so it fails last, and
    the SubtitleEditOcr error names the input file for diagnosis.
    """
    return ".sup" if codec_name == "hdmv_pgs_subtitle" else ".mkv"


def run_subtitle_ocr(
    mkv_path: str,
    stream_index: int,
    video_out_path: str,
    *,
    workdir: str,
    codec_name: str = "",
    se: SubtitleEditOcr | None = None,
    extract_binary: str = "ffmpeg",
    se_runner: Runner | None = None,
) -> OcrResult:
    """Full path: extract -> OCR -> normalize -> <video>.srt sidecar.

    codec_name (v0.2.16) is the probed subtitle codec: PGS
    (hdmv_pgs_subtitle; the "" default keeps v0.2.15 callers and tests
    source-compatible) extracts to .sup; DVD VobSub (dvd_subtitle) and
    unknown codecs extract to a single-track .mkv intermediate - the
    only ffmpeg muxer that accepts a dvd_subtitle copy, which SE's
    OCR reads directly (no mkvextract, no new dependency).
    """
    se = se or SubtitleEditOcr()
    os.makedirs(workdir, exist_ok=True)
    if not codec_name:
        # "" is the source-compatible default (v0.2.15 callers/tests):
        # behave as PGS. _intermediate_ext("") itself still maps to .mkv
        # - that is the separate "unknown codec" contract.
        codec_name = "hdmv_pgs_subtitle"

    img_path = (
        os.path.splitext(os.path.basename(mkv_path))[0]
        + _intermediate_ext(codec_name)
    )
    img_path = os.path.join(workdir, img_path)
    # v0.2.16 guard (deviation from the design doc, found by testing
    # real ffmpeg): the pipeline's workdir IS the ripped MKV's own
    # directory, so the VobSub branch derives the intermediate as the
    # input file itself (same stem, same .mkv extension) - ffmpeg then
    # refuses with "Output ... same as Input #0 - exiting" and every
    # real DVD rip would fail extraction. In that one case the
    # intermediate gets a distinguishing suffix; PGS (.sup) never
    # collides, and a non-colliding workdir keeps the design's plain
    # <stem>.mkv name.
    if os.path.normcase(os.path.abspath(img_path)) == os.path.normcase(
        os.path.abspath(mkv_path)
    ):
        img_path = os.path.join(
            workdir,
            os.path.splitext(os.path.basename(mkv_path))[0] + ".vobsub.mkv",
        )
    extract_subtitles(mkv_path, stream_index, img_path, binary=extract_binary)

    srt_in = se.run(img_path, workdir, runner=se_runner)
    with open(srt_in, encoding="utf-8-sig") as fh:
        raw = fh.read()

    try:
        cues = parse_srt(raw)
    except SrtError as exc:
        raise SubtitleOcrError(f"OCR output was not valid SRT: {exc}") from exc
    cues = normalize_cues(cues)
    notes: list[str] = []
    if not cues:
        raise SubtitleOcrError(
            "OCR completed but produced zero cues; the subtitle track is "
            "likely unusable (or Tesseract needs its English data)."
        )
    if len(cues) < 50:
        notes.append(
            f"Only {len(cues)} cues detected; check a few against the film."
        )
    notes.append("Tesseract OCR is ~95-99% accurate; spot-check spelling.")

    sidecar = os.path.splitext(video_out_path)[0] + ".srt"
    write_srt_file(sidecar, cues)
    os.remove(img_path)
    if srt_in != sidecar and os.path.exists(srt_in):
        os.remove(srt_in)
    return OcrResult(srt_path=sidecar, cue_count=len(cues), notes=notes)


def mux_language(lang: str) -> str:
    """ffprobe subtitle language -> MP4 track tag.

    'eng'/'en' -> 'eng'; anything else (incl. '', the v0.2.14
    unconfirmed case) -> 'und'. The OCR pipeline hard-codes
    /ocrlanguage:en, so stamping 'eng' on an unconfirmed stream would
    make the honest case indistinguishable from the confirmed one in
    players' subtitle menus; 'und' is the container's defined
    "undetermined" value and every player renders it fine.
    """
    return "eng" if lang in ("eng", "en") else "und"


def mux_srt_into_mp4(
    video_path: str,
    srt_path: str,
    *,
    language: str = "und",
    binary: str = "ffmpeg",
    runner: Runner | None = None,
) -> str:
    """Embed srt_path as a soft mov_text subtitle track in video_path.

    Muxes to a temp sibling then os.replace()s it over video_path, so a
    failed or interrupted mux can never destroy the finished encode: on
    any failure the temp is deleted and video_path stays bit-identical
    (v0.2.16). mov_text is the only subtitle codec the MP4 container
    supports and the one every player (VLC, Plex, Jellyfin, Apple TV)
    reads from .mp4.
    """
    binary = resolve_ffmpeg(binary)
    tmp = video_path + ".muxing.mp4"  # sibling: same fs -> atomic replace
    if os.path.exists(tmp):
        try:
            os.remove(tmp)  # stale temp from a killed run
        except OSError:
            pass
    cmd = [
        binary, "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
        "-i", video_path, "-i", srt_path,
        "-map", "0", "-map", "1",
        "-c", "copy", "-c:s", "mov_text",
        "-metadata:s:s:0", f"language={mux_language(language)}",
        tmp,
    ]
    if runner is None:
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
        except FileNotFoundError as exc:
            raise SubtitleOcrError(
                f"ffmpeg not found for subtitle muxing: {exc}. Install "
                "ffmpeg from https://ffmpeg.org/download.html and retry."
            ) from exc
    else:
        proc = runner(cmd)
    if proc.returncode != 0 or not os.path.exists(tmp) or (
        os.path.getsize(tmp) == 0
    ):
        try:
            os.remove(tmp)
        except OSError:
            pass
        stderr = (getattr(proc, "stderr", "") or "").strip()
        raise SubtitleOcrError(f"subtitle muxing failed: {stderr[:300]}")
    try:
        os.replace(tmp, video_path)
    except OSError as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise SubtitleOcrError(
            f"subtitle muxing could not replace the MP4: {exc}"
        ) from exc
    return video_path