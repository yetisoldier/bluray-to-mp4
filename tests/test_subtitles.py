"""Tests for SRT parsing/normalization and the Subtitle Edit OCR wrapper."""

import os
import subprocess

import pytest

from bluray_to_mp4.srt import Cue, parse_srt, normalize_cues, write_srt, write_srt_file
from bluray_to_mp4.subtitles import (
    SubtitleEditOcr,
    SubtitleOcrError,
    _OCR_CLEAN_CHARS,
    _count_auto_resolved_cues,
    _intermediate_ext,
    _ocr_quality_note,
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


def _se_runner_factory(srt_text, fixed_text=None):
    """Two-pass seconv stub (v0.2.27 contract).

    Pass 1 (raw OCR: cmd carries /ocrengine:) writes srt_text to the
    /outputfilename inside /outputfolder. Pass 2 (auto-fix: cmd carries
    /fixcommonerrorsrules:) rewrites the input SRT as fixed_text (an
    unchanged copy when fixed_text is None, like a run with nothing to
    guess). Both passes exit 0 - the fail-open paths get their own
    tests with raising runners.
    """
    def runner(cmd):
        out_name = next(
            a for a in cmd if a.startswith("/outputfilename:")
        ).split(":", 1)[1]
        out_dir = next(
            a for a in cmd if a.startswith("/outputfolder:")
        ).split(":", 1)[1]
        out = os.path.join(out_dir, out_name)
        if any(a.startswith("/fixcommonerrorsrules:") for a in cmd):
            with open(cmd[2], encoding="utf-8-sig") as fh:
                raw_text = fh.read()
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(fixed_text if fixed_text is not None else raw_text)
        else:
            with open(out, "w", encoding="utf-8") as fh:
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
    """Pass-1 invocation: legacy /convert syntax, eng (ISO 639-2 -
    Tesseract rejects 'en'), settings seed, pinned output filename,
    overwrite for re-runs."""
    se = SubtitleEditOcr(binary="seconv.exe")
    cmd = se.command(r"C:\rip\movie.sup", r"C:\rip")
    assert cmd[0] == "seconv.exe"
    assert "/convert" in cmd and "subrip" in cmd
    assert any(a == "/ocrengine:tesseract" for a in cmd)
    assert any(a == "/ocrlanguage:eng" for a in cmd)
    assert any(a.startswith("/settings:") for a in cmd)
    assert any(a == "/outputfilename:b2m_ocr_raw.srt" for a in cmd)
    assert any(a.startswith("/outputfolder:") for a in cmd)
    assert "/overwrite" in cmd


def test_se_fix_command_flags():
    """Pass-2 invocation: the headless auto-fix pass with the OCR
    replace-list rule, language pinned to the OCR language, its own
    output filename so the raw pass output is never clobbered."""
    se = SubtitleEditOcr(binary="seconv.exe")
    cmd = se.fix_command(r"C:\rip\b2m_ocr_raw.srt", r"C:\rip")
    assert cmd[0] == "seconv.exe"
    assert "/convert" in cmd and "subrip" in cmd
    assert "/fixcommonerrorsrules:FixCommonOcrErrors" in cmd
    assert "/fce-language:en" in cmd
    assert any(a == "/outputfilename:b2m_ocr_fixed.srt" for a in cmd)
    assert "/overwrite" in cmd


def test_extract_subtitles_failure_raises(tmp_path):
    # Point ffmpeg at a nonexistent input to force failure (or missing ffmpeg
    # entirely on machines without it - both raise SubtitleOcrError).
    with pytest.raises(SubtitleOcrError, match="extraction"):
        extract_subtitles(
            str(tmp_path / "nope.mkv"), 2, str(tmp_path / "out.sup"),
        )


def test_se_not_installed_message(monkeypatch, tmp_path):
    def missing_run(cmd, **kw):
        raise FileNotFoundError("seconv")
    monkeypatch.setattr("bluray_to_mp4.subtitles.subprocess.run", missing_run)
    se = SubtitleEditOcr()
    with pytest.raises(SubtitleOcrError, match="seconv not found"):
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
    # Two unattended passes: pass 1 gets the single-track MKV, pass 2
    # (auto-fix) gets pass 1's raw SRT output.
    assert se_inputs[0] == seen["out"]
    assert se_inputs[1].endswith("b2m_ocr_raw.srt")
    assert len(se_inputs) == 2
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
# v0.2.27: fully unattended OCR (Eric's 2026-10-02 criterion). Two
# headless seconv passes with zero interaction; unknown glyphs are
# best-guessed by the auto-fix pass and the run records the honest
# auto-resolved count (never a silent pass - the run-12 defect).

def test_write_ocr_settings_seed(tmp_path):
    """The /settings: seed exists, parses as JSON, and every key is a
    real seconv 5.2.0 GeneralSection member (so no unknown-key
    warning ever fires)."""
    from bluray_to_mp4.subtitles import SE_CONV_SETTINGS_NAME, write_ocr_settings
    path = str(tmp_path / SE_CONV_SETTINGS_NAME)
    write_ocr_settings(path)
    import json
    with open(path, encoding="utf-8") as fh:
        seed = json.load(fh)
    known = {
        "subtitleLineMaximumLength", "subtitleMinimumDisplayMilliseconds",
        "subtitleMaximumDisplayMilliseconds", "currentFrameRate",
        "defaultFrameRate", "minimumMillisecondsBetweenLines",
        "maxNumberOfLines", "mergeLinesShorterThan",
        "subtitleMaximumCharactersPerSeconds",
        "subtitleOptimalCharactersPerSeconds",
    }
    general = seed["general"]
    assert set(general) <= known  # every key a real seconv schema member
    assert seed["general"]["currentFrameRate"] == 23.976


def test_run_subtitle_ocr_seeds_settings_and_reports_auto_resolved(
    tmp_path, monkeypatch
):
    """The unattended contract, end to end (stubbed se): the settings
    seed is written before OCR, both passes run, and when the auto-fix
    pass changes cue text the result notes the auto-resolved count
    with the best-guess wording (never silent)."""
    monkeypatch.setattr("bluray_to_mp4.subtitles.extract_subtitles", _fake_extract)
    raw_srt = "1\n00:00:01,000 --> 00:00:03,000\nl will sh0w it\n"
    fixed_srt = "1\n00:00:01,000 --> 00:00:03,000\nI will show it\n"
    se_cmds = []
    seed_seen = {}

    def runner(cmd):
        se_cmds.append(cmd)
        if any(a.startswith("/ocrengine:") for a in cmd):
            # Observe the seed at OCR time: run_subtitle_ocr cleans up
            # its artifacts after the sidecar is written.
            seed_seen["path"] = next(
                a for a in cmd if a.startswith("/settings:")
            ).split(":", 1)[1]
            seed_seen["exists"] = os.path.exists(seed_seen["path"])
        return _se_runner_factory(raw_srt, fixed_text=fixed_srt)(cmd)

    result = run_subtitle_ocr(
        str(tmp_path / "movie_t00.mkv"), 2,
        str(tmp_path / "Movie (2019).mp4"),
        workdir=str(tmp_path),
        se_runner=runner,
    )
    # Settings seeded before the first pass, via /settings:.
    assert len(se_cmds) == 2  # raw OCR pass + auto-fix pass
    from bluray_to_mp4.subtitles import SE_CONV_SETTINGS_NAME
    assert seed_seen["path"] == os.path.join(str(tmp_path), SE_CONV_SETTINGS_NAME)
    assert seed_seen["exists"]  # written before OCR ran, not after
    assert any(a == "/settings:" + seed_seen["path"] for a in se_cmds[0])
    # Honest auto-resolve warning names the changed-cue count.
    auto = [n for n in result.notes if "auto-resolved" in n]
    assert auto and "1 of 1" in auto[0]
    assert "best-guess" in auto[0]
    # The fixed text (not the raw) shipped in the sidecar.
    with open(result.srt_path, encoding="utf-8-sig") as fh:
        assert "I will show it" in fh.read()


def test_run_subtitle_ocr_fix_failure_warns_and_ships_raw(
    tmp_path, monkeypatch
):
    """The auto-fix pass failing is NOT an OCR failure: the raw SRT
    ships (sidecar written, cues intact) with an honest warning naming
    the failure - the fail-open contract extends to pass 2."""
    monkeypatch.setattr("bluray_to_mp4.subtitles.extract_subtitles", _fake_extract)
    raw_srt = "1\n00:00:01,000 --> 00:00:03,000\nl will sh0w it\n"

    def runner(cmd):
        if any(a.startswith("/fixcommonerrorsrules:") for a in cmd):
            return subprocess.CompletedProcess(cmd, 1, "", "fix boom")
        return _se_runner_factory(raw_srt)(cmd)

    result = run_subtitle_ocr(
        str(tmp_path / "movie_t00.mkv"), 2,
        str(tmp_path / "Movie.mp4"),
        workdir=str(tmp_path),
        se_runner=runner,
    )
    assert result.cue_count == 1
    assert os.path.exists(result.srt_path)
    with open(result.srt_path, encoding="utf-8-sig") as fh:
        assert "l will sh0w it" in fh.read()  # raw text shipped
    assert any(
        n.startswith("Unattended OCR auto-fix pass failed")
        for n in result.notes
    )
    assert not any("auto-resolved" in n for n in result.notes)


def test_run_subtitle_ocr_hard_failure_still_fail_open(tmp_path, monkeypatch):
    """Pass 1 (raw OCR) failing still raises SubtitleOcrError - the
    existing pipeline fail-open warning path (MP4 ships without
    subtitles) is untouched by the two-pass change."""
    monkeypatch.setattr("bluray_to_mp4.subtitles.extract_subtitles", _fake_extract)

    def runner(cmd):
        if any(a.startswith("/fixcommonerrorsrules:") for a in cmd):
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 1, "", "tesseract missing")

    with pytest.raises(SubtitleOcrError, match="seconv exited with code 1"):
        run_subtitle_ocr(
            str(tmp_path / "movie_t00.mkv"), 2,
            str(tmp_path / "Movie.mp4"),
            workdir=str(tmp_path),
            se_runner=runner,
        )


def test_run_removes_stale_raw_srt_before_pass1(tmp_path):
    """v0.2.29 (Karen's validation): a stale b2m_ocr_raw.srt from a
    killed prior OCR in the same workdir is purged BEFORE pass 1 - a
    seconv pass-1 that exits 0 without writing must raise, never ship
    the stale file (a different title's subtitles) under a success
    path. Symmetric with the fixed_srt cleanup before pass 2."""
    se = SubtitleEditOcr(binary="seconv.exe")
    stale = tmp_path / "b2m_ocr_raw.srt"
    stale.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nstale from another title\n",
        encoding="utf-8",
    )

    def runner(cmd):
        # Pass 1 exits 0 but writes NOTHING (the Karen-found shape).
        if any(a.startswith("/ocrengine:") for a in cmd):
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return _se_runner_factory("")(cmd)

    with pytest.raises(SubtitleOcrError, match="produced no SRT"):
        se.run(str(tmp_path / "in.sup"), str(tmp_path), runner=runner)
    # The stale file was removed before pass 1 - not shipped.
    assert not stale.exists()


def test_run_subtitle_ocr_unparsable_fixed_falls_back_to_raw(
    tmp_path, monkeypatch
):
    """v0.2.29 (Karen's validation): pass 2 exiting 0 with a truncated
    partial write (unparsable 'fixed' SRT) must NOT lose the good raw
    OCR - fail open to it with an honest note, never raise."""
    monkeypatch.setattr("bluray_to_mp4.subtitles.extract_subtitles", _fake_extract)
    raw_srt = "1\n00:00:01,000 --> 00:00:03,000\nl will sh0w it\n"
    # A truncated timing line: parse_srt raises SrtError on it.
    broken_fixed = "1\n00:00:01,000 --> 00:0"

    result = run_subtitle_ocr(
        str(tmp_path / "movie_t00.mkv"), 2,
        str(tmp_path / "Movie.mp4"),
        workdir=str(tmp_path),
        se_runner=_se_runner_factory(raw_srt, fixed_text=broken_fixed),
    )
    assert result.cue_count == 1
    with open(result.srt_path, encoding="utf-8-sig") as fh:
        assert "l will sh0w it" in fh.read()  # raw text shipped
    assert any(
        "auto-fix output was not valid SRT" in n for n in result.notes
    )
    assert not any("auto-resolved" in n for n in result.notes)
    assert not any("auto-fix pass failed" in n for n in result.notes)


def test_se_gui_binary_refused():
    """The SE 5.x GUI has no unattended mode (run 12's wizard): both a
    direct GUI path and a resolved GUI name must be refused before any
    subprocess spawn."""
    for gui in ("SubtitleEdit", "SubtitleEdit.exe", r"C:\SE\SubtitleEdit.exe"):
        with pytest.raises(SubtitleOcrError, match="cannot run unattended OCR"):
            SubtitleEditOcr(binary=gui)


def test_se_resolved_gui_binary_refused(tmp_path, monkeypatch):
    """A machine where only the GUI is installed: resolve_subtitle_edit
    finds SubtitleEdit.exe, and run() must refuse it (not spawn the
    wizard) - the OCR stage then fails open through the pipeline's
    existing warning path."""
    monkeypatch.setattr(
        "bluray_to_mp4.subtitles.resolve_subtitle_edit",
        lambda binary: r"C:\Tools\SubtitleEdit\SubtitleEdit.exe",
    )
    se = SubtitleEditOcr(binary="seconv")
    with pytest.raises(SubtitleOcrError, match="cannot run unattended OCR"):
        se.run("in.sup", "out", runner=lambda cmd: None)


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


# ---------------------------------------------------------------------
# v0.2.30: OCR quality reporting + diagnosis retention. Eric's
# 2026-10-04 report: the run-12/13 OCR text was 'nowhere near
# accurate' - the run now ships an honest quality line and retains its
# inputs in <output-stem>.ocr/ so accuracy can be tuned offline
# without re-ripping the disc. Reporting and retention only; the OCR
# execution itself is unchanged and fail-open stands everywhere.


def test_ocr_clean_char_contract():
    """The documented conservative 'clean' set: ASCII letters, digits,
    spaces/line breaks, basic punctuation, the dash/ellipsis/currency/
    degree marks, and Latin-1 accented letters (plus U+0178, the one
    capital accented letter Latin-1 itself lacks). Everything else -
    pipes, the cent sign, curly quotes, angle brackets, the multiply/
    divide signs parked inside the Latin-1 range, Latin Extended-A -
    marks a cue as garbage."""
    for ch in (
        "aZ9 \n"
        ".,!?'\n\"-:;()/&%$"
        "\u2013\u2014\u2026\u20ac\u00a3\u00b0"
        "\u00e0\u00e1\u00e2\u00e4\u00e8\u00e9\u00ea\u00eb"
        "\u00ee\u00ef\u00f4\u00f6\u00fb\u00fc\u00e7\u00f1"
        "\u00c0\u00c5\u00d6\u00dc\u00df\u00ff\u0178"
    ):
        assert ch in _OCR_CLEAN_CHARS, repr(ch)
    for ch in "|\u00a2\u2018\u2019\u201c<>\u00d7\u00f7\u0100\u2713":
        assert ch not in _OCR_CLEAN_CHARS, repr(ch)


def test_ocr_quality_note_counts_garbage_empty_and_clean():
    """One line, deterministic counts: clean + garbage + empty always
    adds up to the parsed total, percentage rounded."""
    cues = [
        Cue(1000, 2000, "Hello world"),              # clean
        Cue(3000, 4000, "Caf\u00e9 na\u00efve"),     # clean (accented)
        Cue(5000, 6000, "l1|<e me"),                 # garbage (| and <)
        Cue(7000, 8000, "50\u00a2"),                 # garbage (cent)
        Cue(9000, 10000, "\u2019Tis the season"),     # garbage (curly ')
        Cue(11000, 12000, "|"),                       # garbage (pipe)
        Cue(13000, 14000, "<e>"),                     # garbage (angle)
        Cue(15000, 16000, "   "),                     # empty
    ]
    assert _ocr_quality_note(cues) == (
        "OCR quality: 25% of 8 cues clean "
        "(5 cues contain non-English characters, 1 empty)"
    )


def test_ocr_quality_note_all_clean_is_100():
    note = _ocr_quality_note([Cue(0, 1000, "It's fine \u2013 caf\u00e9s")])
    assert note == (
        "OCR quality: 100% of 1 cues clean "
        "(0 cues contain non-English characters, 0 empty)"
    )


QUALITY_SRT = (
    "1\n00:00:01,000 --> 00:00:02,000\nHello world\n\n"
    "2\n00:00:03,000 --> 00:00:04,000\nl1|<e me\n\n"
    "3\n00:00:05,000 --> 00:00:06,000\nIt's a caf\u00e9\n\n"
    "4\n00:00:07,000 --> 00:00:08,000\n50\u00a2 and \u2019quote\u2019\n\n"
    "5\n00:00:09,000 --> 00:00:10,000\n   \n"
)


def test_run_subtitle_ocr_reports_quality_line(tmp_path, monkeypatch):
    """End to end: the run notes carry the one-line quality report over
    the shipped OCR text - counts include the empty cue (they are taken
    before normalization drops it), so the numbers add up to the parsed
    total."""
    monkeypatch.setattr("bluray_to_mp4.subtitles.extract_subtitles", _fake_extract)
    result = run_subtitle_ocr(
        str(tmp_path / "movie_t00.mkv"), 2,
        str(tmp_path / "Movie (2019).mp4"),
        workdir=str(tmp_path),
        se_runner=_se_runner_factory(QUALITY_SRT),
    )
    quality = [n for n in result.notes if n.startswith("OCR quality:")]
    assert quality == [
        "OCR quality: 40% of 5 cues clean "
        "(2 cues contain non-English characters, 1 empty)"
    ]


def test_auto_resolved_split_cues_count_by_timing():
    """v0.2.30 (Karen's validation nit): pass 2 SPLIT one raw cue into
    two fixed cues. The old zip() pairing saw only the first pair and
    undercounted (1); timing pairing counts both split products (the
    second's start_ms is absent from raw, hence changed)."""
    raw = "1\n00:00:01,000 --> 00:00:02,000\nl1|<e th1s\n"
    fixed = (
        "1\n00:00:01,000 --> 00:00:01,500\nl1|<e\n\n"
        "2\n00:00:01,500 --> 00:00:02,000\nth1s\n"
    )
    assert _count_auto_resolved_cues(raw, fixed) == 2


def test_auto_resolved_merge_cues_count_by_timing():
    """Pass 2 MERGED two short raw cues into one fixed cue; the old
    zip() pairing then drifted - the orphaned raw cue got compared
    against an unrelated later fixed cue, under- or over-counting
    (here it counted 2). Timing pairing keeps the honest count: the
    merged cue changed, the untouched tail cue did not."""
    raw = (
        "1\n00:00:01,000 --> 00:00:02,000\nl1|<e\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\nsh0w\n\n"
        "3\n00:00:03,000 --> 00:00:04,000\nit\n"
    )
    fixed = (
        "1\n00:00:01,000 --> 00:00:03,000\nlike\nshow\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nit\n"
    )
    assert _count_auto_resolved_cues(raw, fixed) == 1


def test_auto_resolved_identical_cues_count_zero():
    """Identical text at the same start_ms is NOT auto-resolved work."""
    text = "1\n00:00:01,000 --> 00:00:02,000\nunchanged text\n"
    assert _count_auto_resolved_cues(text, text) == 0


def test_auto_resolved_parse_error_returns_zero():
    """The defensive 0-on-parse-error contract is unchanged by the
    timing-pairing rewrite."""
    good = "1\n00:00:01,000 --> 00:00:02,000\nx\n"
    assert _count_auto_resolved_cues("not an srt at all", good) == 0
    assert _count_auto_resolved_cues(good, "not an srt at all") == 0


def test_ocr_kit_success_path_retains_inputs(tmp_path, monkeypatch):
    """The diagnosis kit lands next to the MP4 as <output-stem>.ocr/
    with BOTH inputs: the extracted track that was fed to seconv (PGS
    .sup here) and the raw pass-1 SRT copied as <stem>.raw.srt (the
    pass-1 text, not the fixed text) - offline re-tuning material.
    Nothing about it is a warning when everything was retained."""
    monkeypatch.setattr("bluray_to_mp4.subtitles.extract_subtitles", _fake_extract)
    raw_srt = "1\n00:00:01,000 --> 00:00:03,000\nl will sh0w it\n"
    fixed_srt = "1\n00:00:01,000 --> 00:00:03,000\nI will show it\n"
    result = run_subtitle_ocr(
        str(tmp_path / "movie_t00.mkv"), 2,
        str(tmp_path / "Movie (2019).mp4"),
        workdir=str(tmp_path),
        se_runner=_se_runner_factory(raw_srt, fixed_text=fixed_srt),
    )
    kit = tmp_path / "Movie (2019).ocr"
    assert (kit / "movie_t00.sup").read_bytes() == b"PGSDATA"
    with open(kit / "Movie (2019).raw.srt", encoding="utf-8-sig") as fh:
        assert fh.read() == raw_srt  # raw pass-1 text, not the fixed
    assert not any("retained except" in n for n in result.notes)


def test_ocr_kit_vobsub_retains_single_track_mkv(tmp_path, monkeypatch):
    """The VobSub kit retains the single-track .mkv that was fed to
    seconv (the only ffmpeg muxer that accepts dvd_subtitle copy)."""
    def fake_extract(mkv, index, out_path, *, binary="ffmpeg"):
        with open(out_path, "wb") as fh:
            fh.write(b"MKV+VOBSUB")
        return out_path

    monkeypatch.setattr("bluray_to_mp4.subtitles.extract_subtitles", fake_extract)
    run_subtitle_ocr(
        str(tmp_path / "movie_t00.mkv"), 7,
        str(tmp_path / "Movie (2019).mp4"),
        workdir=str(tmp_path / "work"),
        codec_name="dvd_subtitle",
        se_runner=_se_runner_factory(
            "1\n00:00:01,000 --> 00:00:03,000\nHello world\n"),
    )
    kit = tmp_path / "Movie (2019).ocr"
    assert (kit / "movie_t00.mkv").read_bytes() == b"MKV+VOBSUB"
    assert (kit / "Movie (2019).raw.srt").is_file()


def test_ocr_kit_failure_path_retains_what_exists(tmp_path, monkeypatch):
    """Fail-open retention: when OCR raises (the pipeline ships the MP4
    without subtitles), the kit is still created from whatever OCR
    inputs exist - a hard pass-1 failure leaves the extracted track
    but no raw SRT."""
    monkeypatch.setattr("bluray_to_mp4.subtitles.extract_subtitles", _fake_extract)

    def runner(cmd):
        if any(a.startswith("/ocrengine:") for a in cmd):
            return subprocess.CompletedProcess(cmd, 1, "", "tesseract died")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with pytest.raises(SubtitleOcrError, match="seconv exited with code 1"):
        run_subtitle_ocr(
            str(tmp_path / "movie_t00.mkv"), 2,
            str(tmp_path / "Movie (2019).mp4"),
            workdir=str(tmp_path),
            se_runner=runner,
        )
    kit = tmp_path / "Movie (2019).ocr"
    assert (kit / "movie_t00.sup").is_file()      # the track was retained
    assert not (kit / "Movie (2019).raw.srt").exists()  # pass 1 wrote none


def test_ocr_kit_zero_cue_failure_keeps_raw_srt(tmp_path, monkeypatch):
    """The zero-cue OCR failure keeps whatever exists: the track AND the
    (empty) raw SRT pass 1 wrote before the cue-count safety net fired."""
    monkeypatch.setattr("bluray_to_mp4.subtitles.extract_subtitles", _fake_extract)
    with pytest.raises(SubtitleOcrError, match="zero cues"):
        run_subtitle_ocr(
            str(tmp_path / "movie_t00.mkv"), 2,
            str(tmp_path / "Movie (2019).mp4"),
            workdir=str(tmp_path),
            se_runner=_se_runner_factory(""),
        )
    kit = tmp_path / "Movie (2019).ocr"
    assert (kit / "movie_t00.sup").is_file()
    raw = kit / "Movie (2019).raw.srt"
    assert raw.is_file() and raw.stat().st_size == 0


def test_ocr_kit_cap_skips_oversized_track(tmp_path, monkeypatch):
    """The subtitle-track copy is capped at 150 MB: a bigger extract is
    skipped with the honest note, while the tiny raw SRT is always
    kept."""
    cap = 150 * 1024 * 1024

    def big_extract(mkv, index, out_path, *, binary="ffmpeg"):
        with open(out_path, "wb") as fh:  # sparse: size, not bytes
            fh.seek(cap)
            fh.write(b"\0")
        return out_path

    monkeypatch.setattr("bluray_to_mp4.subtitles.extract_subtitles", big_extract)
    result = run_subtitle_ocr(
        str(tmp_path / "movie_t00.mkv"), 2,
        str(tmp_path / "Movie (2019).mp4"),
        workdir=str(tmp_path),
        se_runner=_se_runner_factory(
            "1\n00:00:01,000 --> 00:00:03,000\nHello world\n"),
    )
    kit = tmp_path / "Movie (2019).ocr"
    assert not (kit / "movie_t00.sup").exists()  # over the cap: skipped
    assert (kit / "Movie (2019).raw.srt").is_file()  # always kept
    notes = [n for n in result.notes if "retained except the subtitle track" in n]
    assert notes and "> 150 MB cap" in notes[0]
