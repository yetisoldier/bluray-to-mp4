"""Tests for SRT parsing/normalization and the Subtitle Edit OCR wrapper."""

import os
import subprocess

import pytest

from bluray_to_mp4.srt import Cue, parse_srt, normalize_cues, write_srt, write_srt_file
from bluray_to_mp4.subtitles import (
    SubtitleEditOcr,
    SubtitleOcrError,
    _intermediate_ext,
    extract_subtitles,
    mux_language,
    mux_srt_into_mp4,
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
    # Point ffmpeg at a nonexistent input to force failure (or missing ffmpeg
    # entirely on machines without it - both raise SubtitleOcrError).
    with pytest.raises(SubtitleOcrError, match="extraction"):
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


# ---------------------------------------------------------------------
# v0.2.16: DVD VobSub OCR - the codec chooses the extraction container.
# PGS -> .sup (unchanged); VobSub/unknown -> single-track .mkv (the only
# ffmpeg muxer that accepts a dvd_subtitle copy; Subtitle Edit reads
# VobSub from MKV directly - no mkvextract dependency).


def test_intermediate_ext_pgs_stays_sup():
    """PGS regression pin: the Blu-ray path stays byte-identical to
    v0.2.15 - .sup intermediate, same SE input."""
    assert _intermediate_ext("hdmv_pgs_subtitle") == ".sup"


def test_intermediate_ext_dvd_and_unknown():
    """VobSub -> .mkv; unknown codecs (dvb_subtitle, and the '' default
    sentinel aside) also get .mkv - the most permissive container, so
    they fail last and the SE error names the file for diagnosis."""
    assert _intermediate_ext("dvd_subtitle") == ".mkv"
    assert _intermediate_ext("dvb_subtitle") == ".mkv"
    assert _intermediate_ext("") == ".mkv"


def test_run_subtitle_ocr_vobsub_happy_path(tmp_path, monkeypatch):
    """codec_name='dvd_subtitle': the extract stub must receive a .mkv
    out_path, SE must be handed that same .mkv, and the sidecar SRT
    still lands next to the MP4 (single-track MKV hand-off to SE)."""
    seen = {}

    def fake_extract(mkv, index, out_path, *, binary="ffmpeg"):
        seen["out"] = out_path
        with open(out_path, "wb") as fh:
            fh.write(b"MKV+VOBSUB")
        return out_path

    monkeypatch.setattr("bluray_to_mp4.subtitles.extract_subtitles", fake_extract)
    good_srt = "1\n00:00:01,000 --> 00:00:03,000\nHello world\n"
    se_inputs = []

    def se_runner(cmd):
        se_inputs.append(cmd[2])
        return _se_runner_factory(good_srt)(cmd)

    mkv = str(tmp_path / "movie_t00.mkv")
    workdir = str(tmp_path / "work")  # separate dir: no input collision
    video_out = str(tmp_path / "Movie (2019).mp4")

    result = run_subtitle_ocr(
        mkv, 7, video_out,
        workdir=workdir,
        codec_name="dvd_subtitle",
        se_runner=se_runner,
    )
    assert seen["out"].endswith(".mkv")
    assert seen["out"] == os.path.join(workdir, "movie_t00.mkv")
    assert se_inputs == [seen["out"]]  # SE got the single-track MKV
    assert result.srt_path == str(tmp_path / "Movie (2019).srt")
    assert result.cue_count == 1
    assert os.path.exists(result.srt_path)
    assert any("check" in n.lower() for n in result.notes)


def test_run_subtitle_ocr_vobsub_intermediate_avoids_input_collision(
    tmp_path, monkeypatch
):
    """The pipeline's workdir IS the ripped MKV's own directory, so the
    VobSub branch would derive the intermediate as the input file itself
    (same stem + .mkv) - ffmpeg refuses 'Output same as Input'. In that
    one case the intermediate must take a distinguishing suffix; the
    input MKV must survive the OCR (the intermediate is removed, the
    rip is not)."""
    seen = {}

    def fake_extract(mkv, index, out_path, *, binary="ffmpeg"):
        seen["out"] = out_path
        with open(out_path, "wb") as fh:
            fh.write(b"MKV+VOBSUB")
        return out_path

    monkeypatch.setattr("bluray_to_mp4.subtitles.extract_subtitles", fake_extract)
    good_srt = "1\n00:00:01,000 --> 00:00:03,000\nHello world\n"
    mkv = str(tmp_path / "movie_t00.mkv")
    with open(mkv, "wb") as fh:
        fh.write(b"the precious ripped MKV")

    run_subtitle_ocr(
        mkv, 7, str(tmp_path / "Movie (2019).mp4"),
        workdir=str(tmp_path),  # same dir as the input: collision case
        codec_name="dvd_subtitle",
        se_runner=_se_runner_factory(good_srt),
    )
    assert seen["out"].endswith(".vobsub.mkv")
    assert seen["out"] != mkv
    # The intermediate is cleaned up; the input rip is untouched.
    assert os.path.exists(mkv)
    with open(mkv, "rb") as fh:
        assert fh.read() == b"the precious ripped MKV"
    assert not os.path.exists(seen["out"])


def test_run_subtitle_ocr_default_codec_uses_sup(tmp_path, monkeypatch):
    """No codec_name -> .sup intermediate (source compatibility for
    every v0.2.15 caller and test)."""
    seen = {}

    def fake_extract(mkv, index, out_path, *, binary="ffmpeg"):
        seen["out"] = out_path
        with open(out_path, "wb") as fh:
            fh.write(b"PGSDATA")
        return out_path

    monkeypatch.setattr("bluray_to_mp4.subtitles.extract_subtitles", fake_extract)
    good_srt = "1\n00:00:01,000 --> 00:00:03,000\nHello world\n"
    run_subtitle_ocr(
        str(tmp_path / "movie_t00.mkv"), 2, str(tmp_path / "Movie.mp4"),
        workdir=str(tmp_path),
        se_runner=_se_runner_factory(good_srt),
    )
    assert seen["out"].endswith(".sup")


def test_run_subtitle_ocr_vobsub_zero_cues_raises(tmp_path, monkeypatch):
    """VobSub branch, SE produces an empty SRT -> zero-cue safety net
    fires exactly as on the PGS path (never ship an empty subtitle)."""
    monkeypatch.setattr("bluray_to_mp4.subtitles.extract_subtitles", _fake_extract)
    with pytest.raises(SubtitleOcrError, match="zero cues"):
        run_subtitle_ocr(
            str(tmp_path / "movie_t00.mkv"), 7, str(tmp_path / "Movie.mp4"),
            workdir=str(tmp_path / "work"),
            codec_name="dvd_subtitle",
            se_runner=_se_runner_factory(""),
        )


# ---------------------------------------------------------------------
# v0.2.16: embedded MP4 subtitles - the sidecar SRT is muxed into the
# finished MP4 as a soft mov_text track (temp-then-replace, so a failed
# mux can never destroy the encode; the sidecar always stays on disk).


def test_mux_language_eng_and_und():
    """'eng'/'en' -> 'eng'; everything else (incl. '', the v0.2.14
    unconfirmed-scan case) -> 'und' - honest metadata, never a lie."""
    assert mux_language("eng") == "eng"
    assert mux_language("en") == "eng"
    assert mux_language("") == "und"
    assert mux_language("deu") == "und"


def test_mux_srt_into_mp4_command_shape(tmp_path):
    """ffmpeg argv: both inputs, -map 0 before -map 1, -c copy plus
    -c:s mov_text, language=eng metadata, temp .muxing.mp4 output that
    is atomically replaced onto the video path; sidecar untouched."""
    video = tmp_path / "Movie (2019).mp4"
    video.write_bytes(b"ORIGINAL ENCODE")
    srt = tmp_path / "Movie (2019).srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:03,000\nHi\n", encoding="utf-8")
    cmds = []

    def runner(cmd):
        cmds.append(cmd)
        out = cmd[-1]
        assert out.endswith(".muxing.mp4") and out.startswith(str(video))
        with open(out, "wb") as fh:
            fh.write(b"ENCODE + MOVED MOVSUB")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    result = mux_srt_into_mp4(
        str(video), str(srt), language="eng", runner=runner,
    )
    assert result == str(video)
    cmd = cmds[0]
    assert cmd[cmd.index("-i") + 1] == str(video)
    assert cmd[cmd.index("-i", cmd.index("-i") + 1) + 1] == str(srt)
    maps = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"]
    assert maps == ["0", "1"]  # every MP4 stream, then the SRT appended
    assert "-c" in cmd and cmd[cmd.index("-c") + 1] == "copy"
    assert "-c:s" in cmd and cmd[cmd.index("-c:s") + 1] == "mov_text"
    assert "-metadata:s:s:0" in cmd
    assert cmd[cmd.index("-metadata:s:s:0") + 1] == "language=eng"
    # The temp replaced the original atomically; sidecar still there.
    assert video.read_bytes() == b"ENCODE + MOVED MOVSUB"
    assert srt.exists()
    assert not (tmp_path / (video.name + ".muxing.mp4")).exists()


def test_mux_srt_failure_preserves_original_mp4(tmp_path):
    """returncode 1 -> SubtitleOcrError; the MP4 stays bit-identical,
    no .muxing.mp4 temp is left behind, and the sidecar survives."""
    video = tmp_path / "Movie.mp4"
    original = b"ORIGINAL ENCODE - MUST SURVIVE"
    video.write_bytes(original)
    srt = tmp_path / "Movie.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:03,000\nHi\n", encoding="utf-8")

    def runner(cmd):
        return subprocess.CompletedProcess(
            cmd, 1, "", "Could not find tag for codec mov_text"
        )

    with pytest.raises(SubtitleOcrError, match="muxing failed"):
        mux_srt_into_mp4(str(video), str(srt), runner=runner)
    assert video.read_bytes() == original
    assert not (tmp_path / (video.name + ".muxing.mp4")).exists()
    assert srt.exists()


def test_mux_srt_missing_ffmpeg_message(tmp_path, monkeypatch):
    """runner=None + subprocess.run raising FileNotFoundError -> the
    hint-rich 'ffmpeg not found for subtitle muxing' error; MP4 kept."""
    def missing_run(cmd, **kw):
        raise FileNotFoundError("No such file or directory: 'ffmpeg'")

    monkeypatch.setattr("bluray_to_mp4.subtitles.subprocess.run", missing_run)
    video = tmp_path / "Movie.mp4"
    original = b"ORIGINAL ENCODE"
    video.write_bytes(original)
    srt = tmp_path / "Movie.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:03,000\nHi\n", encoding="utf-8")
    with pytest.raises(SubtitleOcrError, match="ffmpeg not found for subtitle muxing"):
        mux_srt_into_mp4(str(video), str(srt))
    assert video.read_bytes() == original