"""Tests for SRT parsing/normalization and the Subtitle Edit OCR wrapper."""

import os
import subprocess

import pytest

from bluray_to_mp4.srt import Cue, parse_srt, normalize_cues, write_srt, write_srt_file
from bluray_to_mp4.subtitles import (
    SubtitleEditOcr,
    SubtitleOcrError,
    extract_subtitles,
    run_subtitle_ocr,
)

SAMPLE_SRT = """1
00:00:05,000 --> 00:00:07,500
In the beginning,
there was light.

2
00:00:08 --> 00:00:09,250
Second cue, missing ms.
"""


def test_parse_srt_tolerant():
    cues = parse_srt(SAMPLE_SRT)
    assert len(cues) == 2
    assert cues[0].start_ms == 5000 and cues[0].end_ms == 7500
    assert "light" in cues[0].text
    # '00:00:08' parses with zero ms.
    assert cues[1].start_ms == 8000


def test_parse_srt_bom_and_crlf():
    text = "﻿1\r\n00:00:01,000 --> 00:00:02,000\r\nHello\r\n"
    cues = parse_srt(text)
    assert cues == [Cue(1000, 2000, "Hello")]


def test_normalize_drops_empty_and_fixes_durations():
    cues = [
        Cue(10000, 9000, "inverted"),   # gets swapped
        Cue(20000, 20000, "zero"),       # gets 1s duration
        Cue(30000, 31000, "   "),        # dropped (empty)
        Cue(5000, 6000, "first"),        # sorted to front
    ]
    out = normalize_cues(cues)
    assert [c.text for c in out] == ["first", "inverted", "zero"]
    assert out[1].start_ms == 9000 and out[1].end_ms == 10000
    assert out[2].end_ms - out[2].start_ms == 1000


def test_write_srt_roundtrip(tmp_path):
    cues = [Cue(1000, 2500, "Hello"), Cue(4000, 6000, "World")]
    text = write_srt(cues)
    assert "00:00:01,000 --> 00:00:02,500" in text
    reparsed = parse_srt(text)
    assert reparsed == normalize_cues(cues)
    write_srt_file(str(tmp_path / "out.srt"), cues)
    with open(tmp_path / "out.srt", encoding="utf-8-sig") as fh:
        assert parse_srt(fh.read()) == normalize_cues(cues)


def _fake_extract(mkv, index, out_path, *, binary="ffmpeg"):
    with open(out_path, "wb") as fh:
        fh.write(b"PGSDATA")
    return out_path


def _se_runner_factory(srt_text):
    def runner(cmd):
        # SubtitleEdit /convert <in> subrip /ocrengine... /outputfolder:...
        in_path = cmd[2]
        out_dir = cmd[-1].split(":", 1)[1] if ":" in cmd[-1] else "."
        out = os.path.splitext(os.path.basename(in_path))[0] + ".srt"
        with open(os.path.join(out_dir, out), "w", encoding="utf-8") as fh:
            fh.write(srt_text)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return runner


def test_run_subtitle_ocr_happy_path(tmp_path, monkeypatch):
    monkeypatch.setattr("bluray_to_mp4.subtitles.extract_subtitles", _fake_extract)
    good_srt = "1\n00:00:01,000 --> 00:00:03,000\nHello world\n"
    mkv = str(tmp_path / "movie_t00.mkv")
    video_out = str(tmp_path / "Movie (2019).mp4")

    result = run_subtitle_ocr(
        mkv, 2, video_out,
        workdir=str(tmp_path),
        se_runner=_se_runner_factory(good_srt),
    )
    assert result.srt_path == str(tmp_path / "Movie (2019).srt")
    assert result.cue_count == 1
    assert os.path.exists(result.srt_path)
    # Low cue count -> a review note is attached.
    assert any("check" in n.lower() for n in result.notes)


def test_run_subtitle_ocr_zero_cues_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("bluray_to_mp4.subtitles.extract_subtitles", _fake_extract)
    with pytest.raises(SubtitleOcrError, match="zero cues"):
        run_subtitle_ocr(
            str(tmp_path / "m.mkv"), 2, str(tmp_path / "m.mp4"),
            workdir=str(tmp_path),
            se_runner=_se_runner_factory(""),
        )


def test_se_command_flags():
    se = SubtitleEditOcr(binary="SubtitleEdit.exe")
    cmd = se.command(r"C:\rip\movie.sup", r"C:\rip")
    assert cmd[0] == "SubtitleEdit.exe"
    assert "/convert" in cmd and "subrip" in cmd
    assert any(a == "/ocrengine:tesseract" for a in cmd)
    assert any(a == "/ocrlanguage:en" for a in cmd)
    assert cmd[-1].startswith("/outputfolder:")


def test_extract_subtitles_failure_raises(tmp_path):
    # Point ffmpeg at a nonexistent input to force failure.
    with pytest.raises(SubtitleOcrError, match="extraction failed"):
        extract_subtitles(
            str(tmp_path / "nope.mkv"), 2, str(tmp_path / "out.sup"),
        )


def test_se_not_installed_message(monkeypatch, tmp_path):
    def missing_run(cmd, **kw):
        raise FileNotFoundError("SubtitleEdit")
    monkeypatch.setattr("bluray_to_mp4.subtitles.subprocess.run", missing_run)
    se = SubtitleEditOcr()
    with pytest.raises(SubtitleOcrError, match="Subtitle Edit not found"):
        se.run(str(tmp_path / "in.sup"), str(tmp_path), runner=None)