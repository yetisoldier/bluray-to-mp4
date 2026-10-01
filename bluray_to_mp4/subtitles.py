"""Subtitle extraction and OCR: PGS/VOBsub from ripped MKV to sidecar SRT.

Blu-ray subtitles are image-based (PGS) and MP4 cannot contain them, so
the recommended track is extracted to .sup/.sub and OCR'd with Subtitle
Edit's command-line converter (Tesseract engine). The SRT output is then
parsed and normalized locally; a zero-cue result is treated as an OCR
failure and never silently shipped.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

from .srt import Cue, SrtError, normalize_cues, parse_srt, write_srt_file

__all__ = [
    "SubtitleOcrError",
    "OcrResult",
    "SubtitleEditOcr",
    "extract_subtitles",
    "run_subtitle_ocr",
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
    if result.returncode != 0 or not os.path.exists(out_path):
        raise SubtitleOcrError(
            f"subtitle extraction failed: {result.stderr.strip()[:300]}"
        )
    return out_path


@dataclass
class SubtitleEditOcr:
    """OCR via the Subtitle Edit command-line converter (Tesseract)."""

    binary: str = "SubtitleEdit"
    ocr_engine: str = "tesseract"
    language: str = "en"

    def command(self, in_path: str, out_dir: str) -> list[str]:
        """SE CLI: /convert <pattern> <format> /ocrengine:<engine>.

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
        if runner is None:
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                )
            except FileNotFoundError as exc:
                raise SubtitleOcrError(
                    "Subtitle Edit not found on PATH. Install it from "
                    "https://github.com/SubtitleEdit/subtitleedit/releases "
                    "(and Tesseract via the UB Mannheim build), then retry. "
                    f"Details: {exc}"
                ) from exc
            if proc.returncode != 0:
                raise SubtitleOcrError(
                    f"Subtitle Edit exited with code {proc.returncode}: "
                    f"{proc.stderr.strip()[:300]}"
                )
        else:
            proc = runner(cmd)
            if proc.returncode != 0:
                raise SubtitleOcrError(
                    f"Subtitle Edit exited with code {proc.returncode}"
                )

        expected = os.path.splitext(os.path.basename(in_path))[0] + ".srt"
        produced = os.path.join(out_dir, expected)
        if not os.path.exists(produced):
            raise SubtitleOcrError(
                f"Subtitle Edit produced no SRT (expected {produced}). "
                "Check the Tesseract installation."
            )
        return produced


def run_subtitle_ocr(
    mkv_path: str,
    stream_index: int,
    video_out_path: str,
    *,
    workdir: str,
    se: SubtitleEditOcr | None = None,
    extract_binary: str = "ffmpeg",
    se_runner: Runner | None = None,
) -> OcrResult:
    """Full path: extract -> OCR -> normalize -> <video>.srt sidecar."""
    se = se or SubtitleEditOcr()
    os.makedirs(workdir, exist_ok=True)

    sup_path = os.path.splitext(os.path.basename(mkv_path))[0] + ".sup"
    sup_path = os.path.join(workdir, sup_path)
    extract_subtitles(mkv_path, stream_index, sup_path, binary=extract_binary)

    srt_in = se.run(sup_path, workdir, runner=se_runner)
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
    os.remove(sup_path)
    if srt_in != sidecar and os.path.exists(srt_in):
        os.remove(srt_in)
    return OcrResult(srt_path=sidecar, cue_count=len(cues), notes=notes)