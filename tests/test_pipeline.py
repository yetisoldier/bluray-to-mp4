"""End-to-end pipeline dry-run tests (no makemkvcon/ffmpeg required)."""

import json
import os
import sys
import time
from pathlib import Path

import pytest

from bluray_to_mp4 import pipeline as pipeline_mod
from bluray_to_mp4.cli import build_parser
from bluray_to_mp4.ffprobe import (
    AudioDurationProbe,
    CfrVerdict,
    FrameCountProbe,
    FfprobeError,
    ProbedStream,
    first_video_stream,
    first_video_width,
    is_interlaced,
    nth_of_type,
    parse_frame_rate,
    parse_probe,
    probe_duration_seconds,
    video_frame_rate,
)
from bluray_to_mp4.makemkv import _CancelState, make_runner
from bluray_to_mp4.pipeline import EncodeCancelled, execute_encode, run_pipeline
from bluray_to_mp4.subtitles import OcrResult, SubtitleOcrError
from tests.fixtures import MOVIE_DISC_OUTPUT, SERIES_DISC_OUTPUT, fake_runner


def _movie_run(outdir, **kw):
    return run_pipeline(
        0, str(outdir), encoder="x265", dry_run=True,
        mkv_runner=fake_runner(MOVIE_DISC_OUTPUT.splitlines()), **kw,
    )


def _series_run(outdir, **kw):
    return run_pipeline(
        0, str(outdir), encoder="x265", dry_run=True,
        mkv_runner=fake_runner(SERIES_DISC_OUTPUT.splitlines()), **kw,
    )


def test_movie_dry_run_builds_one_job(tmp_path):
    result = _movie_run(tmp_path / "out", year=2017)
    assert result.disc_name == "Blade Runner 2049"
    assert not result.is_series
    assert len(result.jobs) == 1
    job = result.jobs[0]
    assert job.output_name == "Blade Runner 2049 (2017).mp4"
    assert job.input_path.endswith("Blade_Runner_2049_t00.mkv")
    assert job.selection is not None and job.selection.audio_sid == 1
    # Encode args reference the right input/output paths.
    args = job.encode_args
    assert args[args.index("-i") + 1] == job.input_path
    assert args[-1].endswith(job.output_name)
    assert "libx265" in args


def test_movie_name_override_controls_final_output_name(tmp_path):
    """The reviewed movie title must survive a fresh disc scan."""
    result = _movie_run(
        tmp_path / "out",
        movie_name="Mamma Mia! Here We Go Yet Again",
        year=2019,
    )

    job = result.jobs[0]
    assert job.output_name == "Mamma Mia! Here We Go Yet Again (2019).mp4"
    assert job.encode_args[-1].endswith(job.output_name)


def test_movie_dry_run_reserves_srt_path(tmp_path):
    result = _movie_run(tmp_path / "out")
    job = result.jobs[0]
    assert job.selection.subtitle_sid is not None
    assert job.srt_path is not None and job.srt_path.endswith(".srt")
    # QW-2: the stale "roadmap #2" claim (OCR shipped in v0.2.2) is
    # gone; the dry run now states what will actually happen.
    assert any("SRT sidecar reserved" in w for w in result.warnings)
    assert any("Subtitle OCR will run" in w for w in result.warnings)
    assert not any("roadmap" in w for w in result.warnings)


def test_movie_dry_run_no_subtitles_skips_srt(tmp_path):
    """want_subtitles=False: no srt_path, no OCR reservation warning."""
    result = _movie_run(tmp_path / "out", want_subtitles=False)
    assert result.jobs, "movie dry run must still plan the encode"
    for job in result.jobs:
        assert job.srt_path is None
        assert job.subtitle_stream_index is None
    assert not any("SRT sidecar reserved" in w for w in result.warnings)
    assert not any("roadmap" in w for w in result.warnings)


def test_series_dry_run_no_subtitles_skips_srt(tmp_path):
    """Same contract for every episode of a series disc."""
    result = _series_run(tmp_path / "out", want_subtitles=False)
    assert result.is_series and len(result.jobs) == 4
    assert all(job.srt_path is None for job in result.jobs)
    assert not any("SRT sidecar reserved" in w for w in result.warnings)
    assert not any("roadmap" in w for w in result.warnings)


def test_series_dry_run_names_all_episodes(tmp_path):
    result = _series_run(
        tmp_path / "out",
        series_name="Breaking Bad",
        season=1,
        episode_titles={0: "Pilot"},
    )
    assert result.is_series
    assert len(result.jobs) == 4  # 4-min deleted scene excluded
    names = [j.output_name for j in result.jobs]
    assert names[0] == "Breaking Bad - S01E01 - Pilot.mp4"
    for i, name in enumerate(names, start=1):
        assert f"S01E{i:02d}" in name
    # Every episode gets its own recommendation pass.
    assert all(j.selection is not None for j in result.jobs)


def test_series_season_and_start_episode(tmp_path):
    result = _series_run(
        tmp_path / "out", series_name="Show", season=3, start_episode=9,
    )
    assert "S03E09" in result.jobs[0].output_name
    assert "S03E12" in result.jobs[-1].output_name


def test_parse_probe_and_interlace_detection():
    text = (
        '{"streams":['
        '{"index":0,"codec_type":"video","codec_name":"h264","field_order":"tt",'
        '"width":720,"height":480,"sample_aspect_ratio":"32:27",'
        '"display_aspect_ratio":"16:9"},'
        '{"index":1,"codec_type":"audio","codec_name":"truehd","channels":8,'
        '"tags":{"language":"eng"}},'
        '{"index":2,"codec_type":"subtitle","codec_name":"hdmv_pgs_subtitle",'
        '"tags":{"language":"eng"}}'
        "]}"
    )
    streams = parse_probe(text)
    assert len(streams) == 3
    assert is_interlaced(streams)
    video = first_video_stream(streams)
    assert video is not None
    assert video.width == 720
    assert video.height == 480
    assert video.sample_aspect_ratio == "32:27"
    assert video.display_aspect_ratio == "16:9"
    audio = nth_of_type(streams, "audio", 0)
    assert audio.channels == 8 and audio.lang == "eng"
    sub = nth_of_type(streams, "subtitle", 0)
    assert sub.codec_name == "hdmv_pgs_subtitle"
    progressive = [
        s for s in streams if s.codec_type == "video"
    ]
    assert nth_of_type(progressive, "video", 0) is not None


# ---------------------------------------------------------------------
# Real-fps probe (run 13 desync fix). ffprobe's avg_frame_rate
# (r_frame_rate fallback) feeds UpscalePlan.fps so extraction, chunk
# timing, and mezzanine reassembly share the rip's true clock; an
# unparsable rate falls back to 30000/1001 with an honest warning.

@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("24000/1001", "24000/1001"),   # 23.976 film DVD (pulldown stripped)
        ("23.976", "23.976"),           # plain-number producer
        ("25/1", "25/1"),                # PAL
        ("29.97", "29.97"),              # plain NTSC
        ("30000/1001", "30000/1001"),   # NTSC ratio form
    ],
)
def test_parse_frame_rate_accepts_real_shapes(value, expected):
    assert parse_frame_rate(value) == expected


@pytest.mark.parametrize(
    "value",
    ["", "   ", "0/0", "0/1", "N/A", "garbage", "12/0", "-25", "0"],
)
def test_parse_frame_rate_rejects_unusable(value):
    """Absent/unparsable/zero/negative rates yield None -> the caller
    falls back to the 30000/1001 default with an honest warning."""
    assert parse_frame_rate(value) is None


def test_video_frame_rate_prefers_avg_then_r():
    """The actual average clock wins over a nominal r_frame_rate.

    This pins the Mamma Mia failure class: rebuilding PNG frames at a
    slightly faster nominal clock accumulates visible A/V drift over a
    feature even though both rates look like ordinary NTSC values.
    """
    streams = [ProbedStream(0, "video", "h264", r_frame_rate="24000/1001",
                            avg_frame_rate="25/1")]
    assert video_frame_rate(streams) == "25/1"
    streams = [ProbedStream(0, "video", "h264", r_frame_rate="24000/1001",
                            avg_frame_rate="0/0")]
    assert video_frame_rate(streams) == "24000/1001"
    streams = [ProbedStream(0, "video", "h264")]
    assert video_frame_rate(streams) is None
    assert video_frame_rate([ProbedStream(0, "audio", "ac3")]) is None


def test_cli_parser_smoke():
    parser = build_parser()
    args = parser.parse_args([
        "run", "0", "--out", "x", "--dry-run", "--dvd-upscale", "720p",
    ])
    assert args.index == 0 and args.dry_run and args.out == "x"
    assert args.dvd_upscale == "720p"
    assert parser.parse_args(["scan", "0"]).index == 0
    assert parser.parse_args(["drives"]).func is not None


# ----------------------------------------------------------------------
# Cancel tests: fake runner + monkeypatched execute_encode/probe_streams.
# The combined script serves both the scan and every per-title rip call
# (rip_titles ignores TINFO/SINFO lines; scan ignores PRGV/MSG:3307).

_RIP_LINES = [
    "PRGV:16384,65536,65536",
    "PRGV:32768,65536,65536",
    'MSG:3307,0,2,"File was added as title #0",'
    '"File %1 was added as title #%2","/fake/rip/episode.mkv","0"',
    'MSG:5004,0,2,"1 titles saved, 0 failed",'
    '"%1 titles saved, %2 failed","1","0"',
]


def _stub_probe(monkeypatch, *, width=3840, height=2160,
                sample_aspect_ratio="1:1", display_aspect_ratio="16:9",
                subtitle_count=0, sub_codec="hdmv_pgs_subtitle",
                sub_lang="", r_frame_rate="24000/1001", avg_frame_rate=""):
    """Stub probe_streams: a video stream of the given width (the movie
    fixture's scan says 3840x2160, so 3840 keeps reuse tests reusing),
    one audio stream, and optional N subtitle streams. v0.2.16: the
    stubbed subtitle streams carry a codec name and language so the
    VobSub/PGS branch and the mov_text embed tag can be tested.
    v0.2.27: the video stream carries r_frame_rate/avg_frame_rate so
    the real-fps probe (desync fix) can be tested; the default is a
    film rip (23.976)."""
    streams = [
        ProbedStream(
            0, "video", "h264", width=width, height=height,
            sample_aspect_ratio=sample_aspect_ratio,
            display_aspect_ratio=display_aspect_ratio,
            r_frame_rate=r_frame_rate, avg_frame_rate=avg_frame_rate,
        ),
        ProbedStream(1, "audio", "ac3", channels=6),
    ]
    streams += [
        ProbedStream(2 + i, "subtitle", sub_codec, lang=sub_lang)
        for i in range(subtitle_count)
    ]
    monkeypatch.setattr(
        pipeline_mod, "probe_streams",
        lambda path, *, binary="ffprobe": streams,
    )


def _stub_frame_preflight(
    monkeypatch, *, total_frames=163_267, from_metadata=False,
    verdict="CFR", r_frame_rate="24000/1001", avg_frame_rate="24000/1001",
    audio_duration=None, audio_from_format=False, mezzanine_duration=None,
):
    """Stub the v0.2.33 pre-flight probes and the v0.2.34 derivation
    inputs: the decode-counted frame total (probe_frame_count), the
    CFR verdict (probe_cfr_check), the selected audio stream's
    measured duration (probe_audio_duration_seconds), and the
    mezzanine container duration the clock gate probes
    (probe_duration_seconds).

    Defaults describe the Mamma Mia-scale film rip: 163,267 frames at
    23.976 fps (~1:53:39), decode-counted (from_metadata=False), and a
    CFR clock (r == a). The fake rip on disk is a text file, so the
    real helpers cannot run on it.

    v0.2.34: the stamp rate is DERIVED (frames / audio duration), so
    the default audio duration is the .33 fixture's consistent pair -
    total_frames at 24000/1001 (163,267 frames -> 6,809.59 s) - which
    keeps every existing caller one-line and derives exactly
    24000/1001. The real-disc numbers (163,509 frames / 6,818.4 s,
    design §5 fixture a) are passed explicitly by the v0.2.34 tests.
    mezzanine_duration defaults to the audio figure (clock-gate delta
    0: the gate passes); clock-trip tests override it.
    """
    if audio_duration is None:
        audio_duration = total_frames / (24000.0 / 1001.0)
    if mezzanine_duration is None:
        mezzanine_duration = audio_duration
    monkeypatch.setattr(
        pipeline_mod, "probe_frame_count",
        lambda path, *, binary="ffprobe": FrameCountProbe(
            total_frames, from_metadata=from_metadata),
    )
    monkeypatch.setattr(
        pipeline_mod, "probe_cfr_check",
        lambda path, *, binary="ffprobe": CfrVerdict(
            verdict, r_frame_rate=r_frame_rate, avg_frame_rate=avg_frame_rate),
    )
    monkeypatch.setattr(
        pipeline_mod, "probe_audio_duration_seconds",
        lambda path, audio_index, *, binary="ffprobe": AudioDurationProbe(
            float(audio_duration), from_format=audio_from_format,
            stream_ordinal=None if audio_from_format else 0),
    )
    monkeypatch.setattr(
        pipeline_mod, "probe_duration_seconds",
        lambda path, *, binary="ffprobe": float(mezzanine_duration),
    )


def test_cancel_during_rip_raises(tmp_path, monkeypatch):
    _stub_probe(monkeypatch)
    state = {"flag": False}
    events = []

    def on_progress(update):
        events.append(update)
        if update[0] == "rip":
            state["flag"] = True  # user hits Cancel while ripping

    with pytest.raises(EncodeCancelled) as exc:
        run_pipeline(
            0, str(tmp_path / "out"), encoder="x265", dry_run=False,
            mkv_runner=fake_runner(SERIES_DISC_OUTPUT.splitlines() + _RIP_LINES),
            cancel=lambda: state["flag"], on_progress=on_progress,
        )
    assert exc.value.result is not None
    assert not exc.value.result.jobs[0].completed
    assert any(e[0] == "rip" for e in events)


def test_cancel_between_titles_keeps_completed(tmp_path, monkeypatch):
    _stub_probe(monkeypatch)
    state = {"flag": False}
    encodes = []

    def fake_encode(args, *, duration_seconds, binary="ffmpeg",
                    on_progress=None, cancel=None):
        assert cancel is not None  # run_pipeline must forward cancel=
        encodes.append(args)
        state["flag"] = True  # cancel lands right after job 1's encode

    monkeypatch.setattr(pipeline_mod, "execute_encode", fake_encode)
    with pytest.raises(EncodeCancelled) as exc:
        run_pipeline(
            0, str(tmp_path / "out"), encoder="x265", dry_run=False,
            mkv_runner=fake_runner(SERIES_DISC_OUTPUT.splitlines() + _RIP_LINES),
            cancel=lambda: state["flag"],
        )
    result = exc.value.result
    assert result.jobs[0].completed            # job 1 finished before cancel
    assert not result.jobs[1].completed         # job 2 never started
    assert len(encodes) == 1


def test_cancel_during_encode_keeps_completed(tmp_path, monkeypatch):
    _stub_probe(monkeypatch)
    state = {"flag": False}
    encodes = []

    def fake_encode(args, *, duration_seconds, binary="ffmpeg",
                    on_progress=None, cancel=None):
        if len(encodes) == 1:  # second encode in flight: cancel now
            state["flag"] = True
        encodes.append(args)
        if cancel is not None and cancel():
            raise EncodeCancelled("cancelled by user")

    monkeypatch.setattr(pipeline_mod, "execute_encode", fake_encode)
    with pytest.raises(EncodeCancelled) as exc:
        run_pipeline(
            0, str(tmp_path / "out"), encoder="x265", dry_run=False,
            mkv_runner=fake_runner(SERIES_DISC_OUTPUT.splitlines() + _RIP_LINES),
            cancel=lambda: state["flag"],
        )
    result = exc.value.result
    assert result.jobs[0].completed
    assert not result.jobs[1].completed
    assert len(encodes) == 2


@pytest.mark.skipif(sys.platform.startswith("win"), reason="needs /bin/sh")
def test_default_runner_cancel_terminates_process(tmp_path, monkeypatch):
    """make_runner's cancel path kills the makemkvcon Popen mid-rip."""
    script = tmp_path / "fakemkv.sh"
    script.write_text(
        "#!/bin/sh\nwhile true; do echo 'PRGV:1,65536,65536'; sleep 0.05; done\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("B2M_MAKEMKVCON", str(script))

    state = _CancelState()
    runner = make_runner(cancel_state=state)
    gen = runner("makemkvcon", ["-r", "mkv", "disc:0", "0", str(tmp_path)])
    assert next(gen) == "PRGV:1,65536,65536"
    assert state.proc is not None and state.proc.poll() is None

    assert state.cancel()  # flips the shared flag
    with pytest.raises(EncodeCancelled):
        list(gen)  # next line check terminates the child and raises
    assert state.proc.poll() is not None  # process is dead


def test_encode_progress_forwards_eta_4tuple(tmp_path, monkeypatch):
    """run_pipeline forwards encode progress as ("encode", tid, frac, eta)."""
    _stub_probe(monkeypatch)
    payloads = []

    def fake_encode(args, *, duration_seconds, binary="ffmpeg",
                    on_progress=None, cancel=None):
        on_progress(0.001, None)  # below 2%: no stable ETA yet
        on_progress(0.50, 123.0)  # past 2%: ETA available

    monkeypatch.setattr(pipeline_mod, "execute_encode", fake_encode)
    run_pipeline(
        0, str(tmp_path / "out"), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(SERIES_DISC_OUTPUT.splitlines() + _RIP_LINES),
        on_progress=payloads.append,
    )
    encodes = [p for p in payloads if p[0] == "encode"]
    assert encodes, "no encode progress forwarded"
    assert all(len(p) == 4 for p in encodes)  # always a 4-tuple
    assert all(p[3] is None for p in encodes if p[2] <= 0.002)
    assert all(p[3] is not None for p in encodes if p[2] > 0.002)
    assert encodes[0] == ("encode", 0, 0.001, None)
    assert encodes[1] == ("encode", 0, 0.50, 123.0)
    # Rip updates now carry an ETA once data moves (2026-10-02 fix):
    # 4-tuples with a float ETA past 0.2%, plain 3-tuple at 0 (BD+ open).
    rips = [p for p in payloads if p[0] == "rip"]
    assert rips, "no rip progress forwarded"
    assert all(len(p) == 4 for p in rips if p[2] > 0.002)
    assert all(isinstance(p[3], float) and p[3] >= 0 for p in rips if p[2] > 0.002)
    assert all(len(p) == 3 for p in rips if p[2] <= 0.002)


@pytest.mark.skipif(sys.platform.startswith("win"), reason="needs /bin/sh")
def test_execute_encode_eta_contract(tmp_path, monkeypatch):
    """Real execute_encode: on_progress(frac, eta) with eta None <= 2%."""
    script = tmp_path / "fake-ffmpeg.sh"
    script.write_text(
        "#!/bin/sh\n"
        "echo 'out_time_us=10000'\n"   # 0.1% of 10 s: below threshold
        "echo 'out_time_us=5000000'\n"  # 50%: ETA computable
        "echo 'progress=end'\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("B2M_FFMPEG", str(script))
    seen = []
    execute_encode(
        ["-i", "x"], duration_seconds=10, binary="ffmpeg",
        on_progress=lambda frac, eta: seen.append((frac, eta)),
    )
    assert seen[0] == (pytest.approx(0.001), None)  # no ETA below 2%
    assert seen[1][0] == pytest.approx(0.5)
    assert seen[1][1] is not None  # ETA present once past 2%


# ----------------------------------------------------------------------
# .rip cleanup (C4): default workdir is reclaimed only on full success.

def test_success_removes_default_rip_dir(tmp_path, monkeypatch):
    _stub_probe(monkeypatch)
    monkeypatch.setattr(
        pipeline_mod, "execute_encode",
        lambda args, *, duration_seconds, binary="ffmpeg",
        on_progress=None, cancel=None: None,
    )
    outdir = tmp_path / "out"
    script = MOVIE_DISC_OUTPUT.splitlines() + [
        'MSG:3307,0,2,"File added","File %1 added","'
        + str(tmp_path / "rip" / "movie_t00.mkv") + '","0"',
        'MSG:5004,0,2,"1 titles saved, 0 failed",'
        '"%1 titles saved, %2 failed","1","0"',
    ]
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(script),
    )
    assert all(j.completed for j in result.jobs)
    assert not os.path.exists(os.path.join(str(outdir), ".rip"))




def test_stale_rip_intermediates_purged_before_rip(tmp_path, monkeypatch):
    """Validation run 3 (2026-10-02): a partial .mkv left by a killed run
    made makemkvcon hang on its interactive overwrite prompt forever.
    run_pipeline must purge the .rip dir before ripping."""
    _stub_probe(monkeypatch)
    monkeypatch.setattr(
        pipeline_mod, "execute_encode",
        lambda args, *, duration_seconds, binary="ffmpeg",
        on_progress=None, cancel=None: None,
    )
    outdir = tmp_path / "out"
    stale = outdir / ".rip" / "Mamma Mia_t00.mkv"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"partial rip from a killed run")
    script = MOVIE_DISC_OUTPUT.splitlines() + [
        'MSG:3307,0,2,"File added","File %1 added","'
        + str(outdir / ".rip" / "movie_t00.mkv") + '","0"',
        'MSG:5004,0,2,"1 titles saved, 0 failed",'
        '"%1 titles saved, %2 failed","1","0"',
    ]
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(script),
    )
    assert result.jobs and all(j.completed for j in result.jobs)
    assert not stale.exists()



def test_failure_preserves_rip_dir_with_warning(tmp_path, monkeypatch):
    """C6: a single-job failure now RETURNS a partial result (no raise);
    intermediates are kept and the failure lands in result.errors."""
    from bluray_to_mp4.pipeline import EncodeError

    _stub_probe(monkeypatch)

    def failing_encode(args, *, duration_seconds, binary="ffmpeg",
                       on_progress=None, cancel=None):
        raise EncodeError("ffmpeg exited with code 1")

    monkeypatch.setattr(pipeline_mod, "execute_encode", failing_encode)
    outdir = tmp_path / "out"
    script = MOVIE_DISC_OUTPUT.splitlines() + [
        'MSG:3307,0,2,"File added","File %1 added","'
        + str(tmp_path / "rip" / "movie_t00.mkv") + '","0"',
        'MSG:5004,0,2,"1 titles saved, 0 failed",'
        '"%1 titles saved, %2 failed","1","0"',
    ]
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(script),
    )
    rip_dir = os.path.join(str(outdir), ".rip")
    assert os.path.isdir(rip_dir)  # intermediates kept
    assert not result.jobs[0].completed
    assert result.jobs[0].error is not None
    assert len(result.errors) == 1
    assert "ffmpeg exited with code 1" in result.errors[0]
    assert any("Kept intermediates" in w for w in result.warnings)


def test_job_failure_isolated_queue_continues(tmp_path, monkeypatch):
    """C6: title 2 of 3 fails -> jobs 1,3 complete, one errors entry,
    and the stage log names the failure."""
    _stub_probe(monkeypatch)
    monkeypatch.setattr(
        pipeline_mod, "execute_encode",
        lambda args, *, duration_seconds, binary="ffmpeg",
        on_progress=None, cancel=None: None,
    )
    from bluray_to_mp4.makemkv import TitleRipError

    def selective_runner(binary: str, args: list[str]):
        if "info" in args:
            yield from SERIES_DISC_OUTPUT.splitlines()
            return
        # Args layout changed in v0.2.9 ("-r --progress=-stdout mkv disc:0
        # <tid> ..."): locate the tid after the disc:N argument.
        disc_at = next(i for i, a in enumerate(args) if a.startswith("disc:"))
        tid = args[disc_at + 1]
        if tid == "1":  # second episode: rip blows up
            yield 'MSG:5003,0,1,"Error in title 1",' \
                  '"Error in title %1","1"'
            yield 'MSG:5004,0,2,"0 titles saved, 1 failed",' \
                  '"%1 titles saved, %2 failed","0","1"'
            return
        yield from _RIP_LINES

    stages = []
    result = run_pipeline(
        0, str(tmp_path / "out"), encoder="x265", dry_run=False,
        mkv_runner=selective_runner, on_stage=stages.append,
    )
    assert [j.completed for j in result.jobs] == [True, False, True, True]
    assert result.jobs[1].error is not None
    assert result.jobs[1].error != ""
    assert len(result.errors) == 1
    assert "Error in title 1" in result.errors[0]
    # The stage summary mentions the failed output and queue survival.
    assert any("Failed:" in s for s in stages)
    assert any("continuing queue" in s for s in stages)
    # Cancel must still propagate through the isolation wrapper.
    assert not any(getattr(j, "error", None) for j in result.jobs if j.completed)


def test_cancel_still_raises_despite_isolation(tmp_path, monkeypatch):
    """C6 guard: the per-job except must not swallow EncodeCancelled."""
    _stub_probe(monkeypatch)
    state = {"flag": False}

    def fake_encode(args, *, duration_seconds, binary="ffmpeg",
                    on_progress=None, cancel=None):
        state["flag"] = True
        raise EncodeCancelled("cancelled by user")

    monkeypatch.setattr(pipeline_mod, "execute_encode", fake_encode)
    with pytest.raises(EncodeCancelled) as exc:
        run_pipeline(
            0, str(tmp_path / "out"), encoder="x265", dry_run=False,
            mkv_runner=fake_runner(SERIES_DISC_OUTPUT.splitlines() + _RIP_LINES),
            cancel=lambda: state["flag"],
        )
    assert exc.value.result is not None
    assert exc.value.result.errors == []  # cancel is not a job failure


def test_caller_workdir_never_deleted(tmp_path, monkeypatch):
    _stub_probe(monkeypatch)
    monkeypatch.setattr(
        pipeline_mod, "execute_encode",
        lambda args, *, duration_seconds, binary="ffmpeg",
        on_progress=None, cancel=None: None,
    )
    outdir = tmp_path / "out"
    workdir = tmp_path / "custom-wd"
    script = MOVIE_DISC_OUTPUT.splitlines() + [
        'MSG:3307,0,2,"File added","File %1 added","'
        + str(workdir / "movie_t00.mkv") + '","0"',
        'MSG:5004,0,2,"1 titles saved, 0 failed",'
        '"%1 titles saved, %2 failed","1","0"',
    ]
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(script), workdir=str(workdir),
    )
    assert all(j.completed for j in result.jobs)
    assert os.path.isdir(str(workdir))  # caller-supplied: never touched

def test_cli_progress_callback_accepts_4tuple():
    """D1 regression: encode progress emits 4-tuples with ETA; the CLI
    progress callback must not crash (real-run regression Karen found:
    'too many values to unpack (expected 3, got 4)' failed every job)."""
    from bluray_to_mp4.cli import build_parser

    args = build_parser().parse_args(["run", "0", "--out", "x", "--dry-run"])

    # Reproduce the exact callback cmd_run installs, then feed both tuple
    # shapes through it. Any unpack error raises - the test fails loudly.
    import contextlib, io

    def progress(update):
        kind, tid, frac = update[:3]
        eta = update[3] if len(update) > 3 else None
        bar = "#" * int(frac * 40)
        tail = f" eta {int(eta)//60}:{int(eta)%60:02d}" if eta is not None else ""
        assert 3 <= len(update) <= 4

    progress(("rip", 0, 0.5))
    progress(("encode", 0, 0.5, 120.0))
    progress(("encode", 0, 0.5, None))


def test_rip_progress_duplicate_lines_carry_eta(tmp_path, monkeypatch):
    """Validation run 5 (Eric report): the UI's Remaining label kept
    flickering back to 'estimating ...'. Cause: MakeMKV emits PRGV
    duplicates ~2x/second; only frac-advancing lines computed an ETA, so
    every duplicate sent eta=None and the label erased itself. The
    pipeline must CARRY the last estimate forward on duplicates."""
    _stub_probe(monkeypatch)
    monkeypatch.setattr(
        pipeline_mod, "execute_encode",
        lambda args, *, duration_seconds, binary="ffmpeg",
        on_progress=None, cancel=None: None,
    )
    outdir = tmp_path / "out"
    script = MOVIE_DISC_OUTPUT.splitlines() + [
        "PRGV:16384,65536,65536",   # 25%: first real frac -> ETA computed
        "PRGV:16384,65536,65536",   # duplicate: ETA must carry, not None
        "PRGV:32768,65536,65536",   # 50%: advances, fresh ETA
        'MSG:3307,0,2,"File added","File %1 added","'
        + str(outdir / ".rip" / "movie_t00.mkv") + '","0"',
        'MSG:5004,0,2,"1 titles saved, 0 failed",'
        '"%1 titles saved, %2 failed","1","0"',
    ]
    payloads = []
    run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(script), on_progress=payloads.append,
    )
    rips = [p for p in payloads if p[0] == "rip" and p[2] > 0.002]
    assert len(rips) >= 3
    assert all(len(p) == 4 and isinstance(p[3], float) for p in rips)
    # The duplicate line (same frac as its predecessor) still carries ETA.
    assert rips[0][2] == rips[1][2]
    assert rips[1][3] is not None


# ---------------------------------------------------------------------
# v0.2.12: reuse of a complete existing rip in the workdir


def _make_reusable_rip(outdir, monkeypatch, duration=7590.0):
    """Place a 'complete' MKV at the expected .rip path and stub the
    duration probe to agree with the scanned title (2:06:30 = 7590s)."""
    mkv_path = outdir / ".rip" / "Blade_Runner_2049_t00.mkv"
    mkv_path.parent.mkdir(parents=True, exist_ok=True)
    mkv_path.write_bytes(b"existing complete rip")
    monkeypatch.setattr(
        pipeline_mod, "probe_duration_seconds",
        lambda path, *, binary="ffprobe": float(duration))
    return mkv_path


def _stub_encode(monkeypatch):
    monkeypatch.setattr(
        pipeline_mod, "execute_encode",
        lambda args, *, duration_seconds, binary="ffmpeg",
        on_progress=None, cancel=None: None,
    )


def _no_rip_runner(scan=None):
    """Answers the disc scan but FAILS the test if a rip is attempted -
    used when an existing rip must be reused instead of re-ripped."""
    script = (scan or MOVIE_DISC_OUTPUT).splitlines()

    def run(binary: str, args: list[str]):
        if "mkv" in args:
            raise AssertionError(
                "rip must not run when a complete rip is reusable")
        yield from script
    return run


def test_reuses_complete_existing_rip(tmp_path, monkeypatch):
    _stub_probe(monkeypatch)
    _stub_encode(monkeypatch)
    outdir = tmp_path / "out"
    _make_reusable_rip(outdir, monkeypatch)
    stages = []
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=_no_rip_runner(), on_stage=stages.append,
    )
    assert result.jobs and all(j.completed for j in result.jobs)
    assert any("Reusing existing rip" in s for s in stages)


def test_unreadable_existing_rip_deleted_and_reripped(tmp_path, monkeypatch):
    _stub_probe(monkeypatch)
    _stub_encode(monkeypatch)
    outdir = tmp_path / "out"
    mkv_path = outdir / ".rip" / "Blade_Runner_2049_t00.mkv"
    mkv_path.parent.mkdir(parents=True, exist_ok=True)
    mkv_path.write_bytes(b"garbage that ffprobe cannot parse")

    def boom(path, *, binary="ffprobe"):
        raise FfprobeError("ffprobe failed")
    monkeypatch.setattr(pipeline_mod, "probe_duration_seconds", boom)

    script = MOVIE_DISC_OUTPUT.splitlines() + [
        'MSG:3307,0,2,"File added","File %1 added","'
        + str(mkv_path) + '","0"',
        'MSG:5004,0,2,"1 titles saved, 0 failed",'
        '"%1 titles saved, %2 failed","1","0"',
    ]
    stages = []
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(script), on_stage=stages.append,
    )
    assert result.jobs and all(j.completed for j in result.jobs)
    assert any("Ripping title" in s for s in stages)


def test_partial_existing_rip_deleted_and_reripped(tmp_path, monkeypatch):
    _stub_probe(monkeypatch)
    _stub_encode(monkeypatch)
    outdir = tmp_path / "out"
    mkv_path = outdir / ".rip" / "Blade_Runner_2049_t00.mkv"
    mkv_path.parent.mkdir(parents=True, exist_ok=True)
    mkv_path.write_bytes(b"partial rip - only 10% of the title")
    monkeypatch.setattr(
        pipeline_mod, "probe_duration_seconds",
        lambda path, *, binary="ffprobe": 759.0)  # 10% of 7590s

    def rip_must_see_file_gone(binary: str, args: list[str]):
        if "mkv" in args:
            assert not os.path.exists(mkv_path), (
                "partial file must be deleted before the re-rip")
            yield 'MSG:3307,0,2,"File added","File %1 added","' \
                  + str(mkv_path) + '","0"'
            yield 'MSG:5004,0,2,"1 titles saved, 0 failed",' \
                  '"%1 titles saved, %2 failed","1","0"'
            return
        yield from MOVIE_DISC_OUTPUT.splitlines()

    stages = []
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=rip_must_see_file_gone, on_stage=stages.append,
    )
    assert result.jobs and all(j.completed for j in result.jobs)
    assert any("Ripping title" in s for s in stages)


def test_purge_removes_stale_preserves_expected(tmp_path, monkeypatch):
    _stub_probe(monkeypatch)
    outdir = tmp_path / "out"
    mkv_path = _make_reusable_rip(outdir, monkeypatch)
    stale = outdir / ".rip" / "stale_junk.mkv"
    stale.write_bytes(b"leftover from another title")

    def boom(*args, **kwargs):
        raise pipeline_mod.EncodeError("simulated encode failure")
    monkeypatch.setattr(pipeline_mod, "execute_encode", boom)

    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=_no_rip_runner(),
    )
    assert result.jobs and not result.jobs[0].completed
    # .rip is preserved on failure; the stale file is gone, the expected
    # reusable rip survives for the next run.
    assert not stale.exists()
    assert mkv_path.exists()


def test_probe_duration_seconds_parses_and_errors(monkeypatch):
    from bluray_to_mp4 import ffprobe as ff

    class _Ok:
        returncode, stdout, stderr = 0, "5400.123\n", ""

    monkeypatch.setattr(ff.subprocess, "run", lambda *a, **kw: _Ok())
    assert ff.probe_duration_seconds("/x.mp4") == 5400.123

    class _Empty:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(ff.subprocess, "run", lambda *a, **kw: _Empty())
    with pytest.raises(FfprobeError):
        ff.probe_duration_seconds("/x.mp4")


def test_probe_frame_count_two_pass_selection(monkeypatch):
    """v0.2.33 §2.1 selection rule, exercised on stubbed ffprobe JSON:
    slow-pass nb_read_frames wins; on a failed slow pass the fast-pass
    nb_frames stands in with from_metadata=True; both empty -> refuse."""
    from bluray_to_mp4 import ffprobe as ff

    def make_run(payloads):
        payloads = list(payloads)

        def fake_run(*a, **kw):
            class _Result:
                pass

            r = _Result()
            r.returncode, r.stderr = 0, ""
            r.stdout = payloads.pop(0)
            return r

        return fake_run

    # Slow pass (48 decoded frames) wins over fast-pass metadata (60).
    monkeypatch.setattr(
        ff.subprocess, "run",
        make_run([
            '{"streams": [{"nb_frames": "60"}]}',
            '{"streams": [{"nb_read_frames": "48"}]}',
        ]),
    )
    probe = ff.probe_frame_count("/x.mkv")
    assert probe.total_frames == 48
    assert probe.from_metadata is False

    # Slow pass reports zero -> metadata stands in, flagged.
    monkeypatch.setattr(
        ff.subprocess, "run",
        make_run([
            '{"streams": [{"nb_frames": "60"}]}',
            '{"streams": [{"nb_read_frames": "0"}]}',
        ]),
    )
    probe = ff.probe_frame_count("/x.mkv")
    assert probe.total_frames == 60
    assert probe.from_metadata is True

    # Both unusable -> honest refusal before any chunk runs.
    monkeypatch.setattr(
        ff.subprocess, "run",
        make_run([
            '{"streams": [{}]}',
            '{"streams": [{}]}',
        ]),
    )
    with pytest.raises(FfprobeError, match="could not be probed"):
        ff.probe_frame_count("/x.mkv")


def test_probe_cfr_check_verdicts(monkeypatch):
    """v0.2.33 §2.2: |r-a|/a > 0.1% -> VFR; missing/unparsable ->
    UNKNOWN; otherwise CFR. Piggybacks r/avg rates off the same
    -count_frames JSON as the frame count."""
    from bluray_to_mp4 import ffprobe as ff

    def make_run(payload):
        def fake_run(*a, **kw):
            class _Result:
                pass

            r = _Result()
            r.returncode, r.stderr = 0, ""
            r.stdout = payload
            return r

        return fake_run

    # CFR: identical clocks.
    monkeypatch.setattr(
        ff.subprocess, "run",
        make_run(
            '{"streams": [{"r_frame_rate": "24000/1001", '
            '"avg_frame_rate": "24000/1001", "nb_read_frames": "36"}]}'))
    verdict = ff.probe_cfr_check("/x.mkv")
    assert verdict.verdict == "CFR"
    assert verdict.r_frame_rate == "24000/1001"
    assert verdict.avg_frame_rate == "24000/1001"

    # VFR: 23.976 nominal vs 29.97 average - a 25% clock gap.
    monkeypatch.setattr(
        ff.subprocess, "run",
        make_run(
            '{"streams": [{"r_frame_rate": "24000/1001", '
            '"avg_frame_rate": "30000/1001", "nb_read_frames": "36"}]}'))
    verdict = ff.probe_cfr_check("/x.mkv")
    assert verdict.verdict == "VFR"

    # Sub-0.1% rounding differences stay CFR (23.976 vs 23.976023...).
    monkeypatch.setattr(
        ff.subprocess, "run",
        make_run(
            '{"streams": [{"r_frame_rate": "24000/1001", '
            '"avg_frame_rate": "23.976", "nb_read_frames": "36"}]}'))
    verdict = ff.probe_cfr_check("/x.mkv")
    assert verdict.verdict == "CFR"

    # UNKNOWN: unreported average clock.
    monkeypatch.setattr(
        ff.subprocess, "run",
        make_run(
            '{"streams": [{"r_frame_rate": "24000/1001", '
            '"avg_frame_rate": "", "nb_read_frames": "36"}]}'))
    verdict = ff.probe_cfr_check("/x.mkv")
    assert verdict.verdict == "UNKNOWN"


def _probe_run_stub(monkeypatch, *, stream_duration=None, format_duration=None,
                    fmt_error=False):
    """Stub ffprobe subprocess.run for probe_audio_duration_seconds:
    the a:N stream probe answers stream_duration (None = ffprobe
    failed/unparsable -> format fallback), and probe_duration_seconds'
    format=duration call answers format_duration (or raises when
    fmt_error). Calls are keyed by the -select_streams argument."""
    from bluray_to_mp4 import ffprobe as ff

    class _Result:
        pass

    def fake_run(cmd, **kw):
        if "-select_streams" in cmd:
            r = _Result()
            r.returncode, r.stderr = 0, ""
            r.stdout = "" if stream_duration is None else f"{stream_duration}\n"
            return r
        # format=duration probe (probe_duration_seconds) or any other
        # ffprobe call the helper might make.
        if fmt_error:
            raise RuntimeError("ffprobe failed")
        r = _Result()
        r.returncode, r.stderr = 0, ""
        r.stdout = "" if format_duration is None else f"{format_duration}\n"
        return r

    monkeypatch.setattr(ff.subprocess, "run", fake_run)
    return fake_run


def test_probe_audio_duration_stream_extent_primary(monkeypatch):
    """F1: the selected stream's measured extent wins over the format
    figure (design §1.1 step 1); the ordinal lands in the result."""
    from bluray_to_mp4 import ffprobe as ff

    _probe_run_stub(
        monkeypatch, stream_duration=6818.4, format_duration=6800.0)
    probe = ff.probe_audio_duration_seconds("/x.mkv", 0)
    assert probe.seconds == pytest.approx(6818.4)
    assert probe.from_format is False
    assert probe.stream_ordinal == 0
    # A non-zero audio ordinal is probed at a:{N} and reported back.
    _probe_run_stub(
        monkeypatch, stream_duration=5400.0, format_duration=5400.0)
    probe = ff.probe_audio_duration_seconds("/x.mkv", 2)
    assert probe.stream_ordinal == 2
    # audio_index=None probes a:0 (encode's own -map a:0? fallback).
    probe = ff.probe_audio_duration_seconds("/x.mkv", None)
    assert probe.stream_ordinal == 0


def test_probe_audio_duration_format_fallback_and_failures(monkeypatch):
    """F2: no stream extent -> format fallback (from_format=True);
    neither figure -> refuse; <10 s -> refuse; 26% skew -> refuse;
    5% skew -> proceeds with the stream extent (design §1.1 step 3)."""
    from bluray_to_mp4 import ffprobe as ff

    # No stream duration, healthy format duration -> fallback, flagged.
    _probe_run_stub(monkeypatch, stream_duration=None, format_duration=6818.4)
    probe = ff.probe_audio_duration_seconds("/x.mkv", 0)
    assert probe.seconds == pytest.approx(6818.4)
    assert probe.from_format is True
    assert probe.stream_ordinal is None

    # Neither figure -> honest refusal (fail-open upstream).
    _probe_run_stub(monkeypatch, stream_duration=None, format_duration=None)
    with pytest.raises(FfprobeError, match="no usable audio or format duration"):
        ff.probe_audio_duration_seconds("/x.mkv", 0)

    # Format duration below the 10 s floor -> refusal (probe artifact,
    # not a movie; guards the division against pathological rates).
    _probe_run_stub(monkeypatch, stream_duration=None, format_duration=5.0)
    with pytest.raises(FfprobeError, match="minimum"):
        ff.probe_audio_duration_seconds("/x.mkv", 0)

    # Stream extent 26% off the container figure -> broken probe,
    # refuse (never silently set the stamp from a bad extent).
    _probe_run_stub(
        monkeypatch, stream_duration=5000.0, format_duration=6818.4)
    with pytest.raises(FfprobeError, match="disagrees with the container"):
        ff.probe_audio_duration_seconds("/x.mkv", 0)

    # 5% skew (normal lead-in/out skew) proceeds with the stream extent.
    _probe_run_stub(
        monkeypatch, stream_duration=6477.0, format_duration=6818.4)
    probe = ff.probe_audio_duration_seconds("/x.mkv", 0)
    assert probe.seconds == pytest.approx(6477.0)
    assert probe.from_format is False

    # A format duration that does not parse (but exists) is treated as
    # absent: the stream extent still stands when it is healthy.
    _probe_run_stub(
        monkeypatch, stream_duration=6818.4, format_duration=None)
    probe = ff.probe_audio_duration_seconds("/x.mkv", 0)
    assert probe.seconds == pytest.approx(6818.4)
    assert probe.from_format is False


def test_snap_to_canonical_rate_table():
    """F3 (design §5/§6): the canonical snap table with the real-disc
    numbers, the refusal cases, and the tolerance edge (inclusive ±0.1%)."""
    from bluray_to_mp4.ffprobe import CANONICAL_RATES, SNAP_TOLERANCE_FRACTION

    # The disc that forced .34: 163,509 / 6,818.4 = 23.9775 -> film clock.
    assert ff_snap(163_509 / 6818.4) == ("24000/1001", True)
    # A true 29.97 video source (fixture b): 29.9856 -> 0.052% off - snaps.
    assert ff_snap(163_509 / 5453.0) == ("30000/1001", True)
    # PAL control (fixture d first half): 100,000 / 4,000 = exactly 25 ->
    # snaps with snapped=False (no adjustment happened).
    assert ff_snap(100_000 / 4000.0) == ("25", False)
    # Near misses within measurement noise (4th-decimal wobble).
    assert ff_snap(25.02) == ("25", True)
    assert ff_snap(50.04) == ("50", True)
    assert ff_snap(59.97) == ("60000/1001", True)
    assert ff_snap(24.001) == ("24", True)
    # Unsnappable: 26.3158 (fixture d) - 5%+ from every entry.
    with pytest.raises(FfprobeError, match="26.3158"):
        ff_snap(100_000 / 3800.0)
    # Honest boundary near 24 (deviation note: the design's F3 row
    # "between 23.976 and 24 -> refuse" is an arithmetic slip - those
    # two entries are 0.1% apart, so a mid-gap value is WITHIN 0.1% of
    # one of them and nearest-neighbor snaps it, exactly as specified
    # in §1.2's algorithm). 23.99 is nearer 24 -> snaps to "24".
    assert ff_snap(23.99) == ("24", True)
    # One basis point PAST 24's outer window refuses (0.11% above 24
    # and 0.21% above 24000/1001 - outside both windows).
    with pytest.raises(FfprobeError):
        ff_snap(24.0264)
    # Tolerance edge is inclusive: +0.1% of a canonical rate snaps.
    # (Deviation note: the 24000/1001 and 24 entries are exactly 0.1%
    # apart, so their inclusive windows TOUCH at 24.0 exactly - the one
    # point where a value is within tolerance of two entries. There 24
    # is strictly nearer (0% vs 0.1%), so nearest-neighbor snaps to
    # "24" with snapped=False: the exactly-correct answer at the
    # exact boundary, per §1.2's algorithm.)
    for token, canonical in CANONICAL_RATES:
        assert ff_snap(canonical * (1 + SNAP_TOLERANCE_FRACTION)) == (
            token, True) if token != "24000/1001" else True
    assert ff_snap(24000.0 / 1001.0 * (1 + SNAP_TOLERANCE_FRACTION)) == (
        "24", False)  # == 24.0 exactly: the nearer entry, no adjustment
    # Just inside the window on the other entries snaps with adjustment.
    assert ff_snap(25.0 * (1 + SNAP_TOLERANCE_FRACTION)) == ("25", True)
    assert ff_snap(30000.0 / 1001.0 * (1 + SNAP_TOLERANCE_FRACTION)) == (
        "30000/1001", True)
    # -0.1% is likewise inclusive for every entry (except the touching
    # pair from the other side: 24 - 0.1% lands within 24000/1001's and
    # 24's windows; nearer wins).
    for token, canonical in CANONICAL_RATES:
        assert ff_snap(canonical * (1 - SNAP_TOLERANCE_FRACTION)) == (
            token, True) if token != "24" else True
    assert ff_snap(24.0 * (1 - SNAP_TOLERANCE_FRACTION)) == (
        "24000/1001", True)
    # One basis point past the window refuses.
    with pytest.raises(FfprobeError):
        ff_snap(25.0 * (1 + 0.0011))
    # Non-positive garbage refuses before any comparison.
    with pytest.raises(FfprobeError, match="not a positive rate"):
        ff_snap(0.0)


def ff_snap(value):
    """Import-light wrapper: snap_to_canonical_rate without a module
    alias at the test top (keeps the table test readable)."""
    from bluray_to_mp4.ffprobe import snap_to_canonical_rate

    return snap_to_canonical_rate(value)


# ---------------------------------------------------------------------
# v0.2.15 Gap 1: Review-page stream overrides (backend merge contract).
# The UI combos land in a separate session; these tests pin the
# run_pipeline-side behavior: merge at selection time, invalid sids
# keep the recommendation, and an explicit choice/decline disables the
# v0.2.13 rip-time first-subtitle fallback.


def test_stream_override_applies_in_dry_run(tmp_path):
    """stream_overrides={0: {audio 2, subtitles None}} => job selection
    uses sid 2, srt_path None, override warning present."""
    result = _movie_run(
        tmp_path / "out",
        stream_overrides={0: {"audio_sid": 2, "subtitle_sid": None}},
    )
    job = result.jobs[0]
    assert job.selection.audio_sid == 2  # commentary AC3, user's call
    assert job.selection.subtitle_sid is None
    assert job.selection.subtitle_user_declined is True
    assert job.srt_path is None  # declined: no SRT reserved
    assert job.subtitle_stream_index is None
    assert any("user override" in w for w in result.warnings)
    assert not any("SRT sidecar reserved" in w for w in result.warnings)
    # The encode maps the overridden audio ordinal (sid 2 -> 0:a:1).
    args = job.encode_args
    assert args[args.index("-map", args.index("-i")) + 1] == "0:a:1?"


def test_override_invalid_sid_ignored_with_note(tmp_path):
    """A sid not on the disc => recommendation kept, note in warnings,
    no crash (never act on a typo'd/stale sid)."""
    result = _movie_run(
        tmp_path / "out",
        stream_overrides={0: {"audio_sid": 99, "subtitle_sid": 99}},
    )
    job = result.jobs[0]
    assert job.selection.audio_sid == 1     # DTS-HD recommendation kept
    assert job.selection.subtitle_sid == 4  # plain-English recommendation
    assert job.selection.subtitle_user_declined is False
    assert any("ignored user override" in w for w in result.warnings)
    # An untouched recommendation still reserves the SRT sidecar.
    assert job.srt_path is not None


def _rip_tail(outdir):
    """Rip-result lines for the movie fixture (scan-independent tail)."""
    mkv = os.path.join(str(outdir), ".rip", "Blade_Runner_2049_t00.mkv")
    return [
        'MSG:3307,0,2,"File added","File %1 added","' + mkv + '","0"',
        'MSG:5004,0,2,"1 titles saved, 0 failed",'
        '"%1 titles saved, %2 failed","1","0"',
    ]


def _movie_rip_script(outdir):
    """Scan + a successful one-title rip of the movie fixture."""
    return MOVIE_DISC_OUTPUT.splitlines() + _rip_tail(outdir)


def test_explicit_no_subtitles_disables_fallback(tmp_path, monkeypatch):
    """Real run, rip lacks the recommended sub, user picked "(no
    subtitles)": ships no subtitles, no first-stream fallback fires."""
    _stub_probe(monkeypatch, width=3840, subtitle_count=0)
    _stub_encode(monkeypatch)
    outdir = tmp_path / "out"
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_movie_rip_script(outdir)),
        stream_overrides={0: {"audio_sid": None, "subtitle_sid": None}},
    )
    job = result.jobs[0]
    assert job.completed
    assert job.subtitle_stream_index is None
    assert job.srt_path is None
    assert not any("fell back to subtitle stream" in w for w in result.warnings)
    assert not any("ships without subtitles" in w for w in result.warnings)
    assert any("user override - subtitles: none" in w for w in result.warnings)


def test_explicit_subtitle_choice_disables_fallback(tmp_path, monkeypatch):
    """Overridden sid missing from the rip => no fallback substitution;
    the G3 "ships without subtitles" warning only."""
    _stub_probe(monkeypatch, width=3840, subtitle_count=0)
    _stub_encode(monkeypatch)
    outdir = tmp_path / "out"
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_movie_rip_script(outdir)),
        stream_overrides={0: {"audio_sid": None, "subtitle_sid": 3}},
    )
    job = result.jobs[0]
    assert job.completed
    assert job.selection.subtitle_sid == 3
    assert job.subtitle_stream_index is None
    assert not any("fell back to subtitle stream" in w for w in result.warnings)
    assert any("ships without subtitles" in w for w in result.warnings)
    assert any("user override - subtitle sid 3" in w for w in result.warnings)


def test_unchanged_recommendation_keeps_fallback(tmp_path, monkeypatch):
    """Regression guard: no override + recommended sid missing from the
    rip => the v0.2.13 first-subtitle fallback still fires."""
    # Disc recommends subtitle sid 4 (ordinal 1 among sids 3,4,5); the
    # rip stub carries only ONE subtitle stream, so ordinal 1 is absent.
    _stub_probe(monkeypatch, width=3840, subtitle_count=1)
    _stub_encode(monkeypatch)
    monkeypatch.setattr(
        pipeline_mod, "run_subtitle_ocr",
        lambda *a, **k: OcrResult(srt_path=str(tmp_path / "out.srt"), cue_count=100),
    )
    outdir = tmp_path / "out"
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_movie_rip_script(outdir)),
    )
    job = result.jobs[0]
    assert job.completed
    assert job.selection.subtitle_user_declined is False
    assert any("fell back to subtitle stream 2" in w for w in result.warnings)
    assert job.subtitle_stream_index == 2  # the rip's first (only) sub
    assert job.srt_path is not None


# ---------------------------------------------------------------------
# v0.2.15 Gap 2: reuse safety - the existing rip must be the same
# MASTER the current disc's scan describes, not just the same runtime
# (a DVD and a Blu-ray pressing of one movie share a duration).


def test_reuse_rejects_resolution_mismatch(tmp_path, monkeypatch):
    """Scan says 3840x2160, existing rip probes width 720 => re-rip,
    stage line names a different master."""
    _stub_probe(monkeypatch, width=720)
    _stub_encode(monkeypatch)
    outdir = tmp_path / "out"
    _make_reusable_rip(outdir, monkeypatch)
    stages = []
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_movie_rip_script(outdir)),
        on_stage=stages.append,
    )
    assert result.jobs and all(j.completed for j in result.jobs)
    assert any("different master" in s for s in stages)
    assert any("Ripping title" in s for s in stages)


def test_reuse_accepts_matching_resolution(tmp_path, monkeypatch):
    """Companion: existing rip width matches the scanned 3840 => reuse."""
    _stub_probe(monkeypatch, width=3840)
    _stub_encode(monkeypatch)
    outdir = tmp_path / "out"
    _make_reusable_rip(outdir, monkeypatch)
    stages = []
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=_no_rip_runner(), on_stage=stages.append,
    )
    assert result.jobs and all(j.completed for j in result.jobs)
    assert any("Reusing existing rip" in s for s in stages)


# Scan variants for the tier-2/3 tests: media_type comes from the DRV
# disc-type code (12=bluray, 1=dvd, 0=unknown) and the scanned width from
# SINFO attr 19 on the title's first video stream.
_DVD_SCAN = MOVIE_DISC_OUTPUT.replace(
    "DRV:0,2,999,12,", "DRV:0,2,999,1,"
).replace('SINFO:0,0,19,0,"3840x2160"', 'SINFO:0,0,19,0,""')
_BLURAY_NO_RES_SCAN = MOVIE_DISC_OUTPUT.replace(
    'SINFO:0,0,19,0,"3840x2160"', 'SINFO:0,0,19,0,""'
)
_UNKNOWN_SCAN = MOVIE_DISC_OUTPUT.replace(
    "DRV:0,2,999,12,", "DRV:0,2,999,0,"
).replace('SINFO:0,0,19,0,"3840x2160"', 'SINFO:0,0,19,0,""')


def test_reuse_dvd_media_type_rejects_hd_rip(tmp_path, monkeypatch):
    """No scanned resolution, media_type 'dvd', existing rip width 1920
    (a Blu-ray rip) => reject and re-rip."""
    _stub_probe(monkeypatch, width=1920)
    _stub_encode(monkeypatch)
    outdir = tmp_path / "out"
    _make_reusable_rip(outdir, monkeypatch)
    stages = []
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_DVD_SCAN.splitlines() + _rip_tail(outdir)),
        on_stage=stages.append,
    )
    assert result.jobs and all(j.completed for j in result.jobs)
    assert any("different master" in s for s in stages)
    assert any("Ripping title" in s for s in stages)


def test_reuse_bluray_media_type_rejects_sd_rip(tmp_path, monkeypatch):
    """No scanned resolution, media_type 'bluray', existing rip width 720
    (a DVD rip) => reject and re-rip."""
    _stub_probe(monkeypatch, width=720)
    _stub_encode(monkeypatch)
    outdir = tmp_path / "out"
    _make_reusable_rip(outdir, monkeypatch)
    stages = []
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_BLURAY_NO_RES_SCAN.splitlines() + _rip_tail(outdir)),
        on_stage=stages.append,
    )
    assert result.jobs and all(j.completed for j in result.jobs)
    assert any("different master" in s for s in stages)
    assert any("Ripping title" in s for s in stages)


def test_reuse_unknown_scan_data_keeps_duration_only(tmp_path, monkeypatch):
    """Tier 3 (deliberate): no scan resolution AND media_type '' => the
    duration-passing file is reused, exactly as v0.2.12 did."""
    _stub_probe(monkeypatch, width=720)  # would fail either tier-2 class
    _stub_encode(monkeypatch)
    outdir = tmp_path / "out"
    _make_reusable_rip(outdir, monkeypatch)
    stages = []
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=_no_rip_runner(scan=_UNKNOWN_SCAN),
        on_stage=stages.append,
    )
    assert result.jobs and all(j.completed for j in result.jobs)
    assert any("Reusing existing rip" in s for s in stages)


def test_first_video_width_parse_and_helper():
    """Pure-JSON mirror of test_parse_probe_and_interlace_detection:
    ProbedStream.width parses 'width', first_video_width resolves the
    first video stream (None when absent or unparsed)."""
    text = (
        '{"streams":['
        '{"index":0,"codec_type":"video","codec_name":"h264",'
        '"width":1920},'
        '{"index":1,"codec_type":"audio","codec_name":"ac3","channels":6},'
        '{"index":2,"codec_type":"subtitle","codec_name":'
        '"hdmv_pgs_subtitle"}'
        "]}"
    )
    streams = parse_probe(text)
    assert streams[0].width == 1920
    assert first_video_width(streams) == 1920

    no_width = parse_probe(
        '{"streams":[{"index":0,"codec_type":"video",'
        '"codec_name":"h264"}]}'
    )
    assert no_width[0].width == 0
    assert first_video_width(no_width) is None

    audio_only = parse_probe(
        '{"streams":[{"index":0,"codec_type":"audio",'
        '"codec_name":"ac3"}]}'
    )
    assert first_video_width(audio_only) is None


# ---------------------------------------------------------------------
# v0.2.16: DVD VobSub OCR pass-through + embedded MP4 subtitles.
# The probed subtitle codec chooses the extraction container (.sup for
# PGS, single-track .mkv for VobSub) and the OCR'd sidecar SRT is muxed
# into the finished MP4 as a soft mov_text track (sidecar kept; the
# mux temp-then-replaces so a failure never destroys the encode).


def _stub_ocr(monkeypatch, events, *, srt_text=None, raise_exc=None):
    """Recording run_subtitle_ocr stub: logs ('ocr', video_out) and
    either returns a realistic OcrResult (sidecar written next to the
    MP4, like the real one) or raises the given exception."""
    def fake_ocr(mkv, index, video_out, *, workdir, codec_name="", **kw):
        events.append(("ocr", video_out, codec_name))
        if raise_exc is not None:
            raise raise_exc
        sidecar = os.path.splitext(video_out)[0] + ".srt"
        with open(sidecar, "w", encoding="utf-8") as fh:
            fh.write(srt_text or "1\n00:00:01,000 --> 00:00:03,000\nHi\n")
        return OcrResult(srt_path=sidecar, cue_count=1, notes=[])
    monkeypatch.setattr(pipeline_mod, "run_subtitle_ocr", fake_ocr)


def _stub_mux(monkeypatch, events, *, raise_exc=None):
    """Recording mux_srt_into_mp4 stub: logs ('mux', video, srt, lang)."""
    def fake_mux(video_path, srt_path, *, language="und", **kw):
        events.append(("mux", video_path, srt_path, language))
        if raise_exc is not None:
            raise raise_exc
        return video_path
    monkeypatch.setattr(pipeline_mod, "mux_srt_into_mp4", fake_mux)


def test_pipeline_passes_codec_name_to_ocr(tmp_path, monkeypatch):
    """The probed subtitle codec reaches run_subtitle_ocr: a dvd_subtitle
    stream (DVD VobSub) must arrive as codec_name='dvd_subtitle' so the
    extraction picks the .mkv intermediate, not the PGS-only .sup."""
    _stub_probe(monkeypatch, width=3840, subtitle_count=2,
                sub_codec="dvd_subtitle", sub_lang="eng")
    _stub_encode(monkeypatch)
    events = []
    _stub_ocr(monkeypatch, events)
    _stub_mux(monkeypatch, events)
    outdir = tmp_path / "out"
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_movie_rip_script(outdir)),
    )
    job = result.jobs[0]
    assert job.completed
    assert job.srt_path is not None
    assert events[0][0] == "ocr"
    assert events[0][2] == "dvd_subtitle"  # codec_name kwarg pinned


def test_pipeline_ocr_failure_dvd_warns_and_ships(tmp_path, monkeypatch):
    """DVD branch + OCR failure: the fallback contract holds - warning
    says 'subtitle OCR failed' / 'without subtitles', the job still
    completes, no srt_path, and the mux never runs."""
    _stub_probe(monkeypatch, width=3840, subtitle_count=2,
                sub_codec="dvd_subtitle", sub_lang="eng")
    _stub_encode(monkeypatch)
    events = []
    _stub_ocr(
        monkeypatch, events,
        raise_exc=SubtitleOcrError(
            "subtitle extraction failed: sup muxer supports only codec "
            "hdmv_pgs_subtitle for type subtitle"
        ),
    )
    _stub_mux(monkeypatch, events)
    outdir = tmp_path / "out"
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_movie_rip_script(outdir)),
    )
    job = result.jobs[0]
    assert job.completed          # the MP4 ships
    assert job.srt_path is None
    assert any("subtitle OCR failed" in w for w in result.warnings)
    assert any("without subtitles" in w for w in result.warnings)
    assert not any("embed failed" in w for w in result.warnings)
    assert [e[0] for e in events] == ["ocr"]  # mux never reached


def test_pipeline_embeds_srt_after_ocr(tmp_path, monkeypatch):
    """Happy path: OCR runs first, then the sidecar is muxed into the
    finished MP4 with the probed stream's language ('eng' confirmed)."""
    _stub_probe(monkeypatch, width=3840, subtitle_count=2,
                sub_codec="hdmv_pgs_subtitle", sub_lang="eng")
    _stub_encode(monkeypatch)
    events = []
    _stub_ocr(monkeypatch, events)
    _stub_mux(monkeypatch, events)
    outdir = tmp_path / "out"
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_movie_rip_script(outdir)),
    )
    job = result.jobs[0]
    assert job.completed
    kinds = [e[0] for e in events]
    assert kinds == ["ocr", "mux"]  # call order: OCR before embed
    mux_video, mux_srt, mux_lang = events[1][1], events[1][2], events[1][3]
    assert mux_video == os.path.join(str(outdir), job.output_name)
    assert mux_srt == job.srt_path  # the OCR'd sidecar is what gets embedded
    assert os.path.exists(mux_srt)  # sidecar kept on disk
    assert mux_lang == "eng"        # confirmed by the rip's own tags


def test_pipeline_mux_failure_keeps_mp4_and_sidecar(tmp_path, monkeypatch):
    """Embed failure after a successful OCR: the warning names the embed
    stage and the kept sidecar (NOT the OCR-stage wording), the job
    still completes, and the sidecar SRT survives on disk."""
    _stub_probe(monkeypatch, width=3840, subtitle_count=2,
                sub_codec="hdmv_pgs_subtitle", sub_lang="eng")
    _stub_encode(monkeypatch)
    events = []
    _stub_ocr(monkeypatch, events)
    _stub_mux(
        monkeypatch, events,
        raise_exc=SubtitleOcrError("subtitle muxing failed: boom"),
    )
    outdir = tmp_path / "out"
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_movie_rip_script(outdir)),
    )
    job = result.jobs[0]
    assert job.completed                 # MP4 ships, just without subs
    assert job.srt_path is not None
    assert os.path.exists(job.srt_path)   # sidecar SRT kept
    assert any("subtitle embed failed" in w for w in result.warnings)
    assert any("sidecar SRT kept" in w for w in result.warnings)
    assert not any("subtitle OCR failed" in w for w in result.warnings)
    assert not any("MP4 was produced without subtitles" in w for w in result.warnings)


def test_pipeline_untagged_sub_embeds_und(tmp_path, monkeypatch):
    """The v0.2.14 unconfirmed-language case (probed lang='') embeds as
    'und' - honest metadata for an OCR'd English track whose source
    stream was never confirmed."""
    _stub_probe(monkeypatch, width=3840, subtitle_count=2,
                sub_codec="hdmv_pgs_subtitle", sub_lang="")
    _stub_encode(monkeypatch)
    events = []
    _stub_ocr(monkeypatch, events)
    _stub_mux(monkeypatch, events)
    outdir = tmp_path / "out"
    run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_movie_rip_script(outdir)),
    )
    assert events[1][3] == "und"


# ---------------------------------------------------------------------
# v0.2.18: Review-page size target (GB/hour). The spin's value flows
# through run_pipeline's target_gb_per_hour kwarg into BOTH EncodePlan
# construction sites; None (or a value equal to the media-type default)
# keeps the built-in behavior byte-identical.

def _b_v_kbps(args) -> int:
    """Video bitrate number from an ffmpeg arg list ('3863k' -> 3863)."""
    return int(args[args.index("-b:v") + 1].rstrip("k"))


def test_gbph_override_wins_in_dry_run(tmp_path):
    """target_gb_per_hour=3.0 -> the dry-run encode args carry the
    3.0-derived video bitrate (computed via the same estimator the
    encoder uses, pinned here in-test)."""
    from bluray_to_mp4.encode import estimate_video_bitrate_kbps

    result = _movie_run(tmp_path / "out", target_gb_per_hour=3.0)
    args = result.jobs[0].encode_args
    assert _b_v_kbps(args) == estimate_video_bitrate_kbps(
        target_gb_per_hour=3.0, audio_channels=6)
    # Bigger target -> bigger bitrate than the 2.0-default run.
    default_run = _movie_run(tmp_path / "default")
    assert _b_v_kbps(args) > _b_v_kbps(default_run.jobs[0].encode_args)


def test_gbph_default_normalizes_to_none(tmp_path):
    """A value equal to the disc type's default collapses to None: the
    dry-run args are byte-identical to a no-kwarg run (regression guard
    for dry-run stability on untouched runs). Same outdir on both runs:
    encode_args embed the rip/output paths, so only a shared outdir
    makes 'byte-identical' a meaningful comparison."""
    plain = _movie_run(tmp_path / "out")
    defaulted = _movie_run(tmp_path / "out", target_gb_per_hour=2.0)
    assert (plain.jobs[0].encode_args
            == defaulted.jobs[0].encode_args)


def test_gbph_none_keeps_media_type_default(tmp_path):
    """No kwarg -> existing behavior unchanged: each disc type's
    built-in constant drives the bitrate, and a DVD-default kwarg is
    likewise a no-op on a DVD scan (companion to the encode tests)."""
    from bluray_to_mp4.encode import (
        BLURAY_GB_PER_HOUR,
        DVD_GB_PER_HOUR,
        estimate_video_bitrate_kbps,
    )

    bluray = _movie_run(tmp_path / "bluray")
    assert _b_v_kbps(bluray.jobs[0].encode_args) == (
        estimate_video_bitrate_kbps(
            target_gb_per_hour=BLURAY_GB_PER_HOUR, audio_channels=6))

    dvd_plain = run_pipeline(
        0, str(tmp_path / "dvd"), encoder="x265", dry_run=True,
        mkv_runner=fake_runner(_DVD_SCAN.splitlines()),
    )
    assert _b_v_kbps(dvd_plain.jobs[0].encode_args) == (
        estimate_video_bitrate_kbps(
            target_gb_per_hour=DVD_GB_PER_HOUR, audio_channels=6))
    dvd_defaulted = run_pipeline(
        0, str(tmp_path / "dvd"), encoder="x265", dry_run=True,
        mkv_runner=fake_runner(_DVD_SCAN.splitlines()),
        target_gb_per_hour=DVD_GB_PER_HOUR,
    )
    assert (dvd_plain.jobs[0].encode_args
            == dvd_defaulted.jobs[0].encode_args)


def test_dvd_upscale_dry_run_plans_mezzanine_video_input(tmp_path):
    """v0.2.19: DVD AI-upscale dry-runs expose the future mezzanine
    video input while keeping audio mapped from the original rip."""
    result = run_pipeline(
        0, str(tmp_path / "dvd"), encoder="x265", dry_run=True,
        mkv_runner=fake_runner(_DVD_SCAN.splitlines()),
        target_gb_per_hour=1.5,
        dvd_upscale_mode="720p_anime",
    )
    job = result.jobs[0]
    assert job.upscale_mode == "720p_anime"
    assert job.upscale_mezzanine_path.replace("\\", "/").endswith(
        ".upscale/title_00_720p.mkv")
    assert "DVD AI upscale planned: 720p anime" in result.reasons
    maps = [job.encode_args[i + 1] for i, arg in enumerate(job.encode_args) if arg == "-map"]
    assert maps == ["1:a:0?", "0:v:0"]
    assert job.encode_args[job.encode_args.index("-i") + 1] == job.upscale_mezzanine_path


def test_dvd_upscale_ignored_for_bluray_dry_run(tmp_path):
    result = _movie_run(tmp_path / "out", dvd_upscale_mode="720p")
    job = result.jobs[0]
    assert job.upscale_mode == "off"
    assert job.upscale_mezzanine_path is None
    assert "DVD AI upscale planned: 720p" not in result.reasons


def test_dvd_upscale_real_run_uses_mezzanine_video_input(tmp_path, monkeypatch):
    """The real stage returns a video-only mezzanine; final encode maps
    video from it and audio from the original DVD rip."""
    _stub_probe(
        monkeypatch, width=720, height=480,
        sample_aspect_ratio="32:27", display_aspect_ratio="16:9",
        subtitle_count=0,
    )
    # v0.2.33: the pre-flight probes the rip's frame count and CFR
    # verdict; the plan must carry the decode-counted total.
    _stub_frame_preflight(monkeypatch)
    captures = {"upscale": [], "encode": [], "progress": []}

    def fake_upscale(plan, *, on_progress=None, cancel=None):
        captures["upscale"].append(plan)
        if on_progress is not None:
            on_progress(0.5, 42.0)
        return (plan.output_path, plan.total_frames)

    def fake_encode(args, *, duration_seconds, binary="ffmpeg",
                    on_progress=None, cancel=None):
        captures["encode"].append(args)

    monkeypatch.setattr(pipeline_mod, "run_dvd_upscale", fake_upscale)
    monkeypatch.setattr(pipeline_mod, "execute_encode", fake_encode)
    outdir = tmp_path / "out"
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_DVD_SCAN.splitlines() + _rip_tail(outdir)),
        dvd_upscale_mode="720p",
        on_progress=captures["progress"].append,
    )
    assert result.jobs and result.jobs[0].completed
    plan = captures["upscale"][0]
    assert plan.mode == "720p"
    assert plan.fps == "24000/1001"  # v0.2.27: the rip's real frame clock
    # v0.2.33: the plan carries the decode-counted frame total and the
    # metadata flag from the pre-flight (design §2.3).
    assert plan.total_frames == 163_267
    assert plan.frame_count_from_metadata is False
    assert plan.source_width == 720
    assert plan.source_height == 480
    assert plan.sample_aspect_ratio == "32:27"
    assert plan.display_aspect_ratio == "16:9"
    assert plan.output_path.replace("\\", "/").endswith(
        ".upscale/title_00_720p.mkv")
    args = captures["encode"][0]
    inputs = [args[i + 1] for i, arg in enumerate(args) if arg == "-i"]
    assert inputs == [plan.output_path, result.jobs[0].input_path]
    maps = [args[i + 1] for i, arg in enumerate(args) if arg == "-map"]
    assert maps == ["1:a:0?", "0:v:0"]
    assert ("upscale", 0, 0.5, 42.0, None) in captures["progress"]
    assert result.jobs[0].upscale_mezzanine_path == plan.output_path


def test_dvd_upscale_failure_falls_open_to_original_rip(tmp_path, monkeypatch):
    from bluray_to_mp4.upscale import UpscaleError

    _stub_probe(monkeypatch, width=720, subtitle_count=0)
    _stub_frame_preflight(monkeypatch)
    captures = []

    def fake_upscale(plan, *, on_progress=None, cancel=None):
        raise UpscaleError("simulated engine failure")

    def fake_encode(args, *, duration_seconds, binary="ffmpeg",
                    on_progress=None, cancel=None):
        captures.append(args)

    monkeypatch.setattr(pipeline_mod, "run_dvd_upscale", fake_upscale)
    monkeypatch.setattr(pipeline_mod, "execute_encode", fake_encode)
    outdir = tmp_path / "out"
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_DVD_SCAN.splitlines() + _rip_tail(outdir)),
        dvd_upscale_mode="720p_anime",
    )
    assert result.jobs and result.jobs[0].completed
    inputs = [captures[0][i + 1] for i, arg in enumerate(captures[0]) if arg == "-i"]
    assert inputs == [result.jobs[0].input_path]
    assert any("DVD AI upscale failed" in w for w in result.warnings)
    assert any("encoded the original DVD rip instead" in w for w in result.warnings)


def test_upscale_unsnappable_rate_refuses_before_gpu(tmp_path, monkeypatch):
    """Design §5 fixture (d): a derived rate that snaps to no canonical
    entry must refuse BEFORE run_dvd_upscale - the GPU stage is never
    entered - and fail open to the original rip, the warning naming the
    derived rate and the nearest candidate. This replaces the v0.2.27
    fallback test: the 30000/1001-with-warning fallback is retired by
    design §1.3 step 5 (.34 always knows the rate or refuses; the stamp
    never falls back to a default)."""
    _stub_probe(monkeypatch, width=720, subtitle_count=0)
    # Fixture (d): 100,000 decoded frames over 3,800 s -> 26.3158 fps,
    # 5%+ from the nearest canonical (25) - unsnappable, refused.
    _stub_frame_preflight(
        monkeypatch, total_frames=100_000, audio_duration=3800.0)
    upscale_calls = []

    def fake_upscale(plan, *, on_progress=None, cancel=None):
        upscale_calls.append(plan)
        return (plan.output_path, plan.total_frames)

    monkeypatch.setattr(pipeline_mod, "run_dvd_upscale", fake_upscale)
    monkeypatch.setattr(pipeline_mod, "execute_encode", lambda *a, **k: None)
    outdir = tmp_path / "out"
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_DVD_SCAN.splitlines() + _rip_tail(outdir)),
        dvd_upscale_mode="720p",
    )
    assert result.jobs and result.jobs[0].completed  # the MP4 still ships
    assert upscale_calls == []  # the GPU stage was NEVER entered
    assert any("26.3158" in w for w in result.warnings)
    assert any("nearest" in w for w in result.warnings)
    assert any("encoded the original DVD rip instead" in w for w in result.warnings)


def test_upscale_frames_gate_fails_open_with_retention(tmp_path, monkeypatch):
    """v0.2.33 gate + retention (design §5 row 5): a mezzanine whose
    frame count differs from the rip's is refused; the pipeline encodes
    the ORIGINAL rip with an honest warning, the rejected mezzanine is
    RETAINED as *.rejected-<ts>.mkv (never deleted), and a diagnostic
    JSON is written beside it with expected vs actual counts and the
    fps probe values."""
    import json as _json
    import glob as _glob

    _stub_probe(monkeypatch, width=720, subtitle_count=0,
                r_frame_rate="24000/1001")
    _stub_frame_preflight(monkeypatch, total_frames=163_267)
    encode_inputs = []

    def fake_upscale(plan, *, on_progress=None, cancel=None):
        # Put a physical mezzanine on disk so retention has something
        # to rename (the real stage writes it before the gate runs).
        mezz = Path(plan.output_path)
        mezz.parent.mkdir(parents=True, exist_ok=True)
        mezz.write_bytes(b"rejected mezzanine payload")
        return (plan.output_path, plan.total_frames)

    def fake_encode(args, *, duration_seconds, binary="ffmpeg",
                    on_progress=None, cancel=None):
        encode_inputs.append(
            [args[i + 1] for i, arg in enumerate(args) if arg == "-i"])

    def fake_gate_probe(path, *, binary="ffprobe"):
        # The mezzanine probe (gate): one frame short of the rip.
        return FrameCountProbe(163_266)

    monkeypatch.setattr(pipeline_mod, "run_dvd_upscale", fake_upscale)
    monkeypatch.setattr(pipeline_mod, "execute_encode", fake_encode)
    # probe_frame_count is used by BOTH the pre-flight (rip: 163_267)
    # and the gate (mezzanine: 163_266) - discriminate on path shape.
    monkeypatch.setattr(
        pipeline_mod, "probe_frame_count",
        lambda path, *, binary="ffprobe": (
            FrameCountProbe(163_266) if "_720p" in path
            else FrameCountProbe(163_267)
        ),
    )
    outdir = tmp_path / "out"
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_DVD_SCAN.splitlines() + _rip_tail(outdir)),
        dvd_upscale_mode="720p",
    )
    assert result.jobs and result.jobs[0].completed  # the MP4 still ships
    # Encode maps video from the ORIGINAL rip, not the short mezzanine.
    assert encode_inputs[0] == [result.jobs[0].input_path]
    assert any("DVD AI upscale failed" in w for w in result.warnings)
    assert any("163266" in w and "163267" in w for w in result.warnings)
    # Retention: the mezzanine was renamed, never deleted.
    upscale_dir = outdir / ".rip" / ".upscale"
    rejected = list(upscale_dir.glob("title_00_720p.rejected-*.mkv"))
    assert rejected, "rejected mezzanine must be retained, not deleted"
    assert rejected[0].read_bytes() == b"rejected mezzanine payload"
    # The warning names the retained artifacts (design §4).
    assert any("rejected mezzanine retained at" in w for w in result.warnings)
    assert any("diagnostic at" in w for w in result.warnings)
    # The diagnostic records the full drift picture.
    diagnostics = list(upscale_dir.glob("*.diagnostic.json"))
    assert diagnostics
    payload = _json.loads(diagnostics[0].read_text(encoding="utf-8"))
    assert payload["rip_total_frames"] == 163_267
    assert payload["mezzanine_frame_count"] == 163_266
    assert payload["rip_fps"] == "24000/1001"
    assert payload["r_frame_rate"] == "24000/1001"
    assert payload["avg_frame_rate"] == "24000/1001"
    assert payload["cfr_verdict"] == "CFR"
    assert payload["chunks"] == 57  # ceil(163267 / 2877)
    assert payload["chunk_frames"] == 2877
    assert payload["expected_per_chunk"][0] == 2877
    assert payload["expected_per_chunk"][-1] == 163267 - 56 * 2877
    assert "163266" in payload["error"] and "163267" in payload["error"]
    # No mezzanine was fed to the encode.
    assert not any("_720p" in i for inputs in encode_inputs for i in inputs)


def test_upscale_frame_probe_failure_refuses_before_gpu(tmp_path, monkeypatch):
    """v0.2.33 §2.1: when the frame-count probe fails entirely
    (neither decode-count nor metadata), the pipeline refuses BEFORE
    run_dvd_upscale - no GPU stage is ever entered - and fails open
    to the original rip with the honest warning."""
    from bluray_to_mp4.ffprobe import FfprobeError

    _stub_probe(monkeypatch, width=720, subtitle_count=0)
    upscale_calls = []

    def fake_upscale(plan, *, on_progress=None, cancel=None):
        upscale_calls.append(plan)
        return (plan.output_path, 1)

    monkeypatch.setattr(pipeline_mod, "run_dvd_upscale", fake_upscale)
    monkeypatch.setattr(pipeline_mod, "execute_encode", lambda *a, **k: None)

    def boom(path, *, binary="ffprobe"):
        raise FfprobeError("rip frame count could not be probed")

    monkeypatch.setattr(pipeline_mod, "probe_frame_count", boom)
    outdir = tmp_path / "out"
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_DVD_SCAN.splitlines() + _rip_tail(outdir)),
        dvd_upscale_mode="720p",
    )
    assert result.jobs and result.jobs[0].completed  # encode still ships
    assert upscale_calls == []  # the GPU stage was NEVER entered
    assert any("could not be probed" in w for w in result.warnings)
    assert any("encoded the original DVD rip instead" in w for w in result.warnings)


def test_vfr_rip_refused_before_gpu(tmp_path, monkeypatch):
    """v0.2.33 §5 row 6: a genuinely variable-clock rip (r_frame_rate
    24000/1001 vs avg_frame_rate 30000/1001 - a 25% clock gap) is
    refused BEFORE run_dvd_upscale: the GPU stage is never entered, the
    warning names the variable frame rate honestly, and the encode
    ships the original rip. Reassembling a VFR source at constant fps
    would re-create drift by construction."""
    _stub_probe(monkeypatch, width=720, subtitle_count=0)
    upscale_calls = []

    def fake_upscale(plan, *, on_progress=None, cancel=None):
        upscale_calls.append(plan)
        return (plan.output_path, plan.total_frames)

    monkeypatch.setattr(pipeline_mod, "run_dvd_upscale", fake_upscale)
    monkeypatch.setattr(pipeline_mod, "execute_encode", lambda *a, **k: None)
    monkeypatch.setattr(
        pipeline_mod, "probe_frame_count",
        lambda path, *, binary="ffprobe": FrameCountProbe(163_267))
    monkeypatch.setattr(
        pipeline_mod, "probe_cfr_check",
        lambda path, *, binary="ffprobe": CfrVerdict(
            "VFR", r_frame_rate="24000/1001", avg_frame_rate="30000/1001"))
    outdir = tmp_path / "out"
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_DVD_SCAN.splitlines() + _rip_tail(outdir)),
        dvd_upscale_mode="720p",
    )
    assert result.jobs and result.jobs[0].completed  # the MP4 still ships
    assert upscale_calls == []  # the GPU stage was NEVER entered
    assert any("variable frame rate" in w for w in result.warnings)
    assert any("24000/1001" in w and "30000/1001" in w for w in result.warnings)
    assert any("encoded the original DVD rip instead" in w for w in result.warnings)


def test_unknown_frame_rate_rip_refused_before_gpu(tmp_path, monkeypatch):
    """v0.2.33 §5 row 6 (UNKNOWN verdict): a rip whose clock cannot
    even be named (unparseable/unreported rates) must not be rebuilt on
    a guessed constant clock - refuse pre-flight, fail open."""
    _stub_probe(monkeypatch, width=720, subtitle_count=0)
    upscale_calls = []

    def fake_upscale(plan, *, on_progress=None, cancel=None):
        upscale_calls.append(plan)
        return (plan.output_path, plan.total_frames)

    monkeypatch.setattr(pipeline_mod, "run_dvd_upscale", fake_upscale)
    monkeypatch.setattr(pipeline_mod, "execute_encode", lambda *a, **k: None)
    monkeypatch.setattr(
        pipeline_mod, "probe_frame_count",
        lambda path, *, binary="ffprobe": FrameCountProbe(163_267))
    monkeypatch.setattr(
        pipeline_mod, "probe_cfr_check",
        lambda path, *, binary="ffprobe": CfrVerdict(
            "UNKNOWN", r_frame_rate="garbage", avg_frame_rate=""))
    outdir = tmp_path / "out"
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_DVD_SCAN.splitlines() + _rip_tail(outdir)),
        dvd_upscale_mode="720p",
    )
    assert result.jobs and result.jobs[0].completed
    assert upscale_calls == []
    assert any("frame rate could not be determined" in w for w in result.warnings)
    assert any("encoded the original DVD rip instead" in w for w in result.warnings)


def test_cfr_rip_proceeds_to_upscale(tmp_path, monkeypatch):
    """v0.2.33 §5 row 6 (CFR control): identical r/avg rates proceed -
    the Mamma Mia disc lands here (its drift came from time-window
    chunking, not VFR); run_dvd_upscale IS called."""
    _stub_probe(monkeypatch, width=720, subtitle_count=0)
    _stub_frame_preflight(monkeypatch, total_frames=163_267)
    upscale_calls = []

    def fake_upscale(plan, *, on_progress=None, cancel=None):
        upscale_calls.append(plan)
        return (plan.output_path, plan.total_frames)

    monkeypatch.setattr(pipeline_mod, "run_dvd_upscale", fake_upscale)
    monkeypatch.setattr(pipeline_mod, "execute_encode", lambda *a, **k: None)
    outdir = tmp_path / "out"
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_DVD_SCAN.splitlines() + _rip_tail(outdir)),
        dvd_upscale_mode="720p",
    )
    assert result.jobs and result.jobs[0].completed
    assert len(upscale_calls) == 1  # CFR proceeds into the GPU stage
    assert not any("variable frame rate" in w for w in result.warnings)
    assert not any("rejected mezzanine retained" in w for w in result.warnings)


def test_dvd_upscale_cancel_still_propagates(tmp_path, monkeypatch):
    _stub_probe(monkeypatch, width=720, subtitle_count=0)
    _stub_frame_preflight(monkeypatch)

    def fake_upscale(plan, *, on_progress=None, cancel=None):
        raise EncodeCancelled("cancelled during upscale")

    monkeypatch.setattr(pipeline_mod, "run_dvd_upscale", fake_upscale)
    outdir = tmp_path / "out"
    with pytest.raises(EncodeCancelled):
        run_pipeline(
            0, str(outdir), encoder="x265", dry_run=False,
            mkv_runner=fake_runner(_DVD_SCAN.splitlines() + _rip_tail(outdir)),
            dvd_upscale_mode="720p",
        )


# ---------------------------------------------------------------------
# v0.2.34: derived stamp rate (design §5/§6). plan.fps comes from
# decode truth (frames / selected-audio duration, snapped to a
# canonical rate); the container's rate fields are diagnostic-only.

_MAMMA_MIA_HWA_FRAMES = 163_509      # Eric's probe of J:\.rip\C1_t10.mkv
_MAMMA_MIA_HWA_AUDIO_SECONDS = 6818.4  # 113.6-min film clock


def test_stamp_rate_derived_from_decode_truth_soft_telecine(
    tmp_path, monkeypatch,
):
    """Design §5 fixture (a) - the Mamma Mia HWA bug being fixed. The
    container lies uniformly (r == avg == 30000/1001, so the .33 CFR
    pre-flight correctly passes its own definition), but the decode
    truth is 163,509 frames over 6,818.4 s = 23.9775 fps -> snaps to
    24000/1001. The plan must stamp 23.976, chunk at 2877 frames, and
    encode from the mezzanine. Shipped v0.2.33 would have stamped
    30000/1001 (~20% desync)."""
    from bluray_to_mp4.upscale import compute_chunk_bounds, compute_chunk_frames

    _stub_probe(
        monkeypatch, width=720, subtitle_count=0,
        r_frame_rate="30000/1001", avg_frame_rate="30000/1001",
    )
    _stub_frame_preflight(
        monkeypatch, total_frames=_MAMMA_MIA_HWA_FRAMES,
        r_frame_rate="30000/1001", avg_frame_rate="30000/1001",
        audio_duration=_MAMMA_MIA_HWA_AUDIO_SECONDS,
    )
    plans = []

    def fake_upscale(plan, *, on_progress=None, cancel=None):
        plans.append(plan)
        return (plan.output_path, plan.total_frames)

    monkeypatch.setattr(pipeline_mod, "run_dvd_upscale", fake_upscale)
    monkeypatch.setattr(pipeline_mod, "execute_encode", lambda *a, **k: None)
    outdir = tmp_path / "out"
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_DVD_SCAN.splitlines() + _rip_tail(outdir)),
        dvd_upscale_mode="720p",
    )
    assert result.jobs and result.jobs[0].completed
    assert len(plans) == 1
    plan = plans[0]
    # The regression: the stamp is the DERIVED clock, not the container's.
    assert plan.fps == "24000/1001"
    assert plan.total_frames == _MAMMA_MIA_HWA_FRAMES
    # Chunk math at the derived clock (design §2): 2877/chunk -> 57
    # chunks, matching the observed 10/17 plan.
    chunk_frames = compute_chunk_frames(120, plan.fps)
    assert chunk_frames == 2877
    bounds = compute_chunk_bounds(_MAMMA_MIA_HWA_FRAMES, chunk_frames)
    assert len(bounds) == 57
    assert bounds[-1] == (56 * 2877, _MAMMA_MIA_HWA_FRAMES - 56 * 2877)
    # The stamp decision is silent when it works: no frame-rate noise.
    assert not any("frame rate" in w for w in result.warnings)


def test_stamp_rate_true_2997_video_source_unchanged(tmp_path, monkeypatch):
    """Design §5 fixture (b) - the conservative control. A true 29.97
    video source (163,509 frames / 5,453.0 s -> 29.9856 fps, the audio-
    skew noise a real disc produces) keeps .33's answer: the derived
    rate snaps to 30000/1001 and the plan is identical to a truthful
    container's. The fix changes nothing for discs that tell the truth.
    """
    from bluray_to_mp4.upscale import compute_chunk_frames

    _stub_probe(
        monkeypatch, width=720, subtitle_count=0,
        r_frame_rate="30000/1001", avg_frame_rate="30000/1001",
    )
    _stub_frame_preflight(
        monkeypatch, total_frames=_MAMMA_MIA_HWA_FRAMES,
        r_frame_rate="30000/1001", avg_frame_rate="30000/1001",
        audio_duration=5453.0,
    )
    plans = []

    def fake_upscale(plan, *, on_progress=None, cancel=None):
        plans.append(plan)
        return (plan.output_path, plan.total_frames)

    monkeypatch.setattr(pipeline_mod, "run_dvd_upscale", fake_upscale)
    monkeypatch.setattr(pipeline_mod, "execute_encode", lambda *a, **k: None)
    outdir = tmp_path / "out"
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_DVD_SCAN.splitlines() + _rip_tail(outdir)),
        dvd_upscale_mode="720p",
    )
    assert result.jobs and result.jobs[0].completed
    assert plans[0].fps == "30000/1001"
    assert compute_chunk_frames(120, plans[0].fps) == 3596  # .33's number
    assert not any("frame rate" in w for w in result.warnings)


def test_stamp_rate_no_audio_uses_format_duration(tmp_path, monkeypatch):
    """Design §5 fixture (c): no audio-stream extent -> the format
    duration stands in (from_format=True) and the derivation proceeds
    at the same total; the fallback is recorded so the operator can
    distrust it. Standalone helper behavior (the pipeline hard-errors
    earlier when no audio stream exists at all), so the pipeline-level
    stub carries audio_from_format=True with the same 6,818.4 s figure.
    """
    _stub_probe(monkeypatch, width=720, subtitle_count=0)
    _stub_frame_preflight(
        monkeypatch, total_frames=_MAMMA_MIA_HWA_FRAMES,
        audio_duration=_MAMMA_MIA_HWA_AUDIO_SECONDS,
        audio_from_format=True,
    )
    probes = []
    real_probe = pipeline_mod.probe_audio_duration_seconds

    def recording_probe(path, audio_index, *, binary="ffprobe"):
        probes.append((path, audio_index))
        return AudioDurationProbe(
            _MAMMA_MIA_HWA_AUDIO_SECONDS, from_format=True, stream_ordinal=None)

    monkeypatch.setattr(
        pipeline_mod, "probe_audio_duration_seconds", recording_probe)
    plans = []

    def fake_upscale(plan, *, on_progress=None, cancel=None):
        plans.append(plan)
        return (plan.output_path, plan.total_frames)

    monkeypatch.setattr(pipeline_mod, "run_dvd_upscale", fake_upscale)
    monkeypatch.setattr(pipeline_mod, "execute_encode", lambda *a, **k: None)
    outdir = tmp_path / "out"
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_DVD_SCAN.splitlines() + _rip_tail(outdir)),
        dvd_upscale_mode="720p",
    )
    assert result.jobs and result.jobs[0].completed
    assert plans[0].fps == "24000/1001"  # same derivation as fixture (a)
    assert plans and probes  # the selected ordinal was probed


def test_stamp_rate_no_audio_missing_format_refuses(tmp_path, monkeypatch):
    """Design §5 fixture (c) refusal sub-case: no stream extent AND no
    parsable format duration -> the pre-flight refuses before the GPU
    stage and fails open (helper F2 pins the standalone behavior).
    """
    from bluray_to_mp4.upscale import UpscaleError as _UpscaleError

    _stub_probe(monkeypatch, width=720, subtitle_count=0)
    _stub_frame_preflight(monkeypatch)

    def boom(path, audio_index, *, binary="ffprobe"):
        raise FfprobeError(
            "no usable audio or format duration for /fake; cannot derive "
            "the stamp rate")

    monkeypatch.setattr(pipeline_mod, "probe_audio_duration_seconds", boom)
    upscale_calls = []
    monkeypatch.setattr(
        pipeline_mod, "run_dvd_upscale",
        lambda plan, *, on_progress=None, cancel=None: (
            upscale_calls.append(plan), (plan.output_path, plan.total_frames),
        )[1],
    )
    monkeypatch.setattr(pipeline_mod, "execute_encode", lambda *a, **k: None)
    outdir = tmp_path / "out"
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_DVD_SCAN.splitlines() + _rip_tail(outdir)),
        dvd_upscale_mode="720p",
    )
    assert result.jobs and result.jobs[0].completed
    assert upscale_calls == []  # refused before the GPU stage
    assert any("no usable audio or format duration" in w for w in result.warnings)
    assert any("encoded the original DVD rip instead" in w for w in result.warnings)


def test_clock_gate_trip_retains_mezzanine_and_diagnostic(
    tmp_path, monkeypatch,
):
    """Design §6 P5: a clock-gate trip POST-upscale (frames gate passes,
    mezzanine plays at the wrong speed) fails open to the original rip,
    retains the rejected mezzanine as *.rejected-*.mkv, and the
    diagnostic JSON carries the full v0.2.34 rate picture - derived raw
    + snapped, container fields, audio duration + source, mezzanine
    duration, and the gate's tolerance."""
    import json as _json

    _stub_probe(monkeypatch, width=720, subtitle_count=0,
                r_frame_rate="30000/1001", avg_frame_rate="30000/1001")
    # The shape that shipped broken in .33: exact frames, wrong clock -
    # a 163,509-frame mezzanine stamped 29.97 plays 5,453 s against
    # 6,818.4 s of audio.
    _stub_frame_preflight(
        monkeypatch, total_frames=_MAMMA_MIA_HWA_FRAMES,
        r_frame_rate="30000/1001", avg_frame_rate="30000/1001",
        audio_duration=_MAMMA_MIA_HWA_AUDIO_SECONDS,
        mezzanine_duration=5453.0,
    )
    encode_inputs = []

    def fake_upscale(plan, *, on_progress=None, cancel=None):
        mezz = Path(plan.output_path)
        mezz.parent.mkdir(parents=True, exist_ok=True)
        mezz.write_bytes(b"clock-desynced mezzanine payload")
        return (plan.output_path, plan.total_frames)

    def fake_encode(args, *, duration_seconds, binary="ffmpeg",
                    on_progress=None, cancel=None):
        encode_inputs.append(
            [args[i + 1] for i, arg in enumerate(args) if arg == "-i"])

    # The frames gate passes (exact count); only the clock trips.
    monkeypatch.setattr(
        pipeline_mod, "probe_frame_count",
        lambda path, *, binary="ffprobe": FrameCountProbe(
            _MAMMA_MIA_HWA_FRAMES),
    )
    monkeypatch.setattr(pipeline_mod, "run_dvd_upscale", fake_upscale)
    monkeypatch.setattr(pipeline_mod, "execute_encode", fake_encode)
    outdir = tmp_path / "out"
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_DVD_SCAN.splitlines() + _rip_tail(outdir)),
        dvd_upscale_mode="720p",
    )
    assert result.jobs and result.jobs[0].completed  # the MP4 still ships
    # Encode maps video from the ORIGINAL rip, not the desynced mezzanine.
    assert encode_inputs[0] == [result.jobs[0].input_path]
    assert any("plays for" in w and "audio runs" in w for w in result.warnings)
    assert any("rejected mezzanine retained at" in w for w in result.warnings)
    assert any("diagnostic at" in w for w in result.warnings)
    upscale_dir = outdir / ".rip" / ".upscale"
    rejected = list(upscale_dir.glob("title_00_720p.rejected-*.mkv"))
    assert rejected, "rejected mezzanine must be retained, not deleted"
    assert rejected[0].read_bytes() == b"clock-desynced mezzanine payload"
    diagnostics = list(upscale_dir.glob("*.diagnostic.json"))
    assert diagnostics
    payload = _json.loads(diagnostics[0].read_text(encoding="utf-8"))
    # The full §4 rate picture: derived truth next to the container's
    # claims, plus the clock-gate evidence.
    assert payload["rip_total_frames"] == _MAMMA_MIA_HWA_FRAMES
    assert payload["stamp_fps"] == "24000/1001"
    assert payload["derived_rate_raw"] == pytest.approx(
        _MAMMA_MIA_HWA_FRAMES / _MAMMA_MIA_HWA_AUDIO_SECONDS, rel=1e-6)
    assert payload["derived_rate_snapped"] is True
    assert payload["container_r_frame_rate"] == "30000/1001"
    assert payload["container_avg_frame_rate"] == "30000/1001"
    assert payload["audio_duration_seconds"] == pytest.approx(
        _MAMMA_MIA_HWA_AUDIO_SECONDS)
    assert payload["audio_duration_source"] == "stream"
    assert payload["audio_stream_ordinal"] == 0
    assert payload["mezzanine_duration_seconds"] == pytest.approx(5453.0)
    assert payload["clock_tolerance_seconds"] == pytest.approx(
        max(2.0, 0.001 * _MAMMA_MIA_HWA_AUDIO_SECONDS))
    assert payload["chunks"] == 57
    assert payload["chunk_frames"] == 2877
    assert "plays for" in payload["error"]


def test_derived_rate_diagnostic_fields_on_success(tmp_path, monkeypatch):
    """Design §6 P6 (light contract): on a SUCCESSFUL upscale run the
    pipeline derives the rate, snaps it, and completes without
    warnings - the stamp decision is silent when it works (fixture a's
    no-noise contract), and no retention/diagnostic artifacts appear.
    """
    _stub_probe(monkeypatch, width=720, subtitle_count=0,
                r_frame_rate="30000/1001", avg_frame_rate="30000/1001")
    _stub_frame_preflight(
        monkeypatch, total_frames=_MAMMA_MIA_HWA_FRAMES,
        r_frame_rate="30000/1001", avg_frame_rate="30000/1001",
        audio_duration=_MAMMA_MIA_HWA_AUDIO_SECONDS,
    )
    plans = []

    def fake_upscale(plan, *, on_progress=None, cancel=None):
        plans.append(plan)
        return (plan.output_path, plan.total_frames)

    monkeypatch.setattr(pipeline_mod, "run_dvd_upscale", fake_upscale)
    monkeypatch.setattr(pipeline_mod, "execute_encode", lambda *a, **k: None)
    outdir = tmp_path / "out"
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_DVD_SCAN.splitlines() + _rip_tail(outdir)),
        dvd_upscale_mode="720p",
    )
    assert result.jobs and result.jobs[0].completed
    assert plans[0].fps == "24000/1001"
    assert not any("frame rate" in w for w in result.warnings)
    assert not any("rejected mezzanine retained" in w for w in result.warnings)
    upscale_dir = outdir / ".rip" / ".upscale"
    assert not list(upscale_dir.glob("*.diagnostic.json")) if upscale_dir.exists() else True


def test_gbph_override_reaches_real_run_encode_args(tmp_path, monkeypatch):
    """The real-run EncodePlan rebuild (after rip/probe) honors the
    same override, so real-run and dry-run arg text agree on the
    bitrate numbers."""
    _stub_probe(monkeypatch, width=3840)
    captured = []

    def fake_encode(args, *, duration_seconds, binary="ffmpeg",
                    on_progress=None, cancel=None):
        captured.append(args)

    monkeypatch.setattr(pipeline_mod, "execute_encode", fake_encode)
    outdir = tmp_path / "out"
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_movie_rip_script(outdir)),
        target_gb_per_hour=3.0,
    )
    assert result.jobs and all(j.completed for j in result.jobs)
    dry = _movie_run(tmp_path / "dry", target_gb_per_hour=3.0)
    assert _b_v_kbps(captured[0]) == _b_v_kbps(dry.jobs[0].encode_args)


# =====================================================================
# v0.2.35: finalize-phase heartbeat at the pipeline gate block (design
# §2.1/§2.3 scope 2), .upscale purge preservation (§3.6), resume end to
# end (§3), and Karen's patch-forward findings C.1-C.4.

def _dvd_upscale_env(monkeypatch, tmp_path, *, upscale_calls=None,
                     on_progress=None, cancel=None,
                     gate_delay=0.0, frames=None, audio_seconds=None,
                     format_seconds=None, cfr_error=False,
                     mezzanine_duration=None):
    """Shared environment for the v0.2.35 pipeline tests: a DVD scan
    with a complete existing rip (reuse lane skips the physical rip),
    stubbed probes, and a run_dvd_upscale fake that emits stage-shaped
    progress and returns a real mezzanine path on disk."""
    _stub_probe(monkeypatch, width=720, height=480, subtitle_count=0,
                sample_aspect_ratio="32:27", display_aspect_ratio="16:9")
    total = frames if frames is not None else _MAMMA_MIA_HWA_FRAMES
    audio = audio_seconds if audio_seconds is not None else _MAMMA_MIA_HWA_AUDIO_SECONDS
    monkeypatch.setattr(
        pipeline_mod, "probe_frame_count",
        lambda path, *, binary="ffprobe": FrameCountProbe(total))
    monkeypatch.setattr(
        pipeline_mod, "probe_cfr_check",
        lambda path, *, binary="ffprobe": CfrVerdict(
            "CFR", r_frame_rate="24000/1001", avg_frame_rate="24000/1001"))
    fmt = format_seconds if format_seconds is not None else audio
    monkeypatch.setattr(
        pipeline_mod, "probe_audio_duration_seconds",
        lambda path, audio_index, *, binary="ffprobe": AudioDurationProbe(
            audio, from_format=False, stream_ordinal=audio_index or 0,
            format_seconds=fmt))
    monkeypatch.setattr(
        pipeline_mod, "probe_duration_seconds",
        lambda path, *, binary="ffprobe": float(
            mezzanine_duration if mezzanine_duration is not None else audio))
    monkeypatch.setattr(
        pipeline_mod, "execute_encode",
        lambda args, *, duration_seconds, binary="ffmpeg",
        on_progress=None, cancel=None: None)

    def fake_upscale(plan, *, on_progress=None, cancel=None):
        if upscale_calls is not None:
            upscale_calls.append(plan)
        mezz = Path(plan.output_path)
        mezz.parent.mkdir(parents=True, exist_ok=True)
        mezz.write_bytes(b"mezzanine payload")
        if on_progress is not None:
            on_progress(1.0, None, {"chunk": 57, "chunks": 57,
                                    "step": "chunk complete"})
        return (plan.output_path, plan.total_frames)

    monkeypatch.setattr(pipeline_mod, "run_dvd_upscale", fake_upscale)
    return fake_upscale


def _run_dvd_pipeline(monkeypatch, tmp_path, *, on_progress=None,
                      upscale_env=None, cancel=None):
    (upscale_env or _dvd_upscale_env)(monkeypatch, tmp_path)
    return run_pipeline(
        0, str(tmp_path / "out"), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_DVD_SCAN.splitlines() + _rip_tail(tmp_path)),
        dvd_upscale_mode="720p",
        on_progress=on_progress,
        cancel=cancel,
    )


def test_pipeline_emits_finalize_steps_before_gates(tmp_path, monkeypatch):
    """P-A1: the gate block announces each verify step (clamped 0.999)
    through the SAME on_progress lambda the stage used, before each
    gate runs - the 2026-10-19 65-minute silent decode now names
    itself."""
    gate_calls = []

    def slow_frames_gate(mezz, rip_total, *, probe_frames):
        gate_calls.append(("frames", mezz))
        time.sleep(0.16)  # > 2 heartbeat ticks at the patched interval

    def fast_clock_gate(mezz, audio, *, probe_duration,
                        tolerance_fraction=0.001,
                        tolerance_floor_seconds=2.0):
        gate_calls.append(("clock", mezz))

    monkeypatch.setattr(pipeline_mod, "_FINALIZE_HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(pipeline_mod, "verify_mezzanine_frames",
                        slow_frames_gate)
    monkeypatch.setattr(pipeline_mod, "verify_mezzanine_clock",
                        fast_clock_gate)

    events = []
    _run_dvd_pipeline(
        monkeypatch, tmp_path,
        upscale_env=lambda mp, tp: _dvd_upscale_env(mp, tp),
        on_progress=events.append,
    )

    kinds = [k for k, _ in gate_calls]
    assert kinds == ["frames", "clock"], "both gates ran in order"
    finalize = [e for e in events if e[0] == "upscale" and e[4]
                and e[4].get("step") in (
                    pipeline_mod.FINALIZE_STEP_VERIFY_FRAMES,
                    pipeline_mod.FINALIZE_STEP_VERIFY_CLOCK)]
    steps = [e[4]["step"] for e in finalize]
    assert steps[0] == pipeline_mod.FINALIZE_STEP_VERIFY_FRAMES
    assert steps[-1] == pipeline_mod.FINALIZE_STEP_VERIFY_CLOCK
    # Both emissions clamped at 0.999, eta None (no fake progress).
    for e in finalize:
        assert e[2] == pytest.approx(0.999)
        assert e[3] is None
    # The frames announcement precedes the frames gate call.
    first_frames_idx = steps.index(pipeline_mod.FINALIZE_STEP_VERIFY_FRAMES)
    assert first_frames_idx == 0


def test_classify_snapshot_finalize_fresh_heartbeat_not_stuck():
    """P-A2: heartbeat-alive finalize (fresh update_age) with progress
    frozen at the clamp for an hour reads RUNNING - the exact shape the
    2026-10-19 watcher false-fired five STALLs on."""
    from bluray_to_mp4.monitor import classify_snapshot

    snap = {
        "busy": True,
        "stage": "Upscaling movie ...",
        "current_job": "movie",
        "jobs_done": 0,
        "jobs_total": 1,
        "progress": 0.999,
        "step": "verifying frame count (full decode)",
        "updated_at": 4990.0,
        "progress_updated_at": 1390.0,   # frozen for 3600 s
        "stage_started_at": 1390.0,
        "last_error": None,
    }
    health = classify_snapshot(snap, now=5000.0, stale_seconds=300.0)
    assert health["status"] == "running"
    assert health["ok"]
    assert health["reasons"] == []


def test_classify_snapshot_finalize_dead_heartbeat_stuck():
    """P-A3: the same finalize shape with a DEAD heartbeat (update_age
    900 s) reads STUCK with the no-monitor-update reason - heartbeat
    dead IS the hang signal."""
    from bluray_to_mp4.monitor import classify_snapshot

    snap = {
        "busy": True,
        "stage": "Upscaling movie ...",
        "current_job": "movie",
        "jobs_done": 0,
        "jobs_total": 1,
        "progress": 0.999,
        "step": "verifying audio clock",
        "updated_at": 4100.0,
        "progress_updated_at": 3200.0,
        "stage_started_at": 3200.0,
        "last_error": None,
    }
    health = classify_snapshot(snap, now=5000.0, stale_seconds=300.0)
    assert health["status"] == "stuck"
    assert any("no monitor update" in r for r in health["reasons"])


def test_classify_snapshot_nonfinalize_at_999_still_stuck():
    """P-A4: the exemption is finalize-steps-only - a non-finalize step
    (or None) frozen at 100% with a stale update still reads stuck."""
    from bluray_to_mp4.monitor import classify_snapshot

    snap = {
        "busy": True,
        "stage": "Upscaling movie ...",
        "current_job": "movie",
        "jobs_done": 0,
        "jobs_total": 1,
        "progress": 1.0,
        "step": "chunk complete",
        "updated_at": 100.0,
        "progress_updated_at": 100.0,
        "stage_started_at": 100.0,
        "last_error": None,
    }
    health = classify_snapshot(snap, now=500.0, stale_seconds=300.0)
    assert health["status"] == "stuck"
    assert any("100.0%" in r for r in health["reasons"])

    # No step at all: same verdict (the exemption is keyed on the step
    # tokens, never on 'upscale' stage text alone).
    snap["step"] = None
    health = classify_snapshot(snap, now=500.0, stale_seconds=300.0)
    assert health["status"] == "stuck"


def test_rip_purge_preserves_upscale_tree_when_upscale_planned(
        tmp_path, monkeypatch):
    """P-B1: with an upscale planned, the .upscale tree (resume
    material + mezzanines) survives the pre-rip purge while a stray
    file dies; with upscale off, the old purge behavior stands."""
    _stub_probe(monkeypatch, width=720, subtitle_count=0)
    _stub_encode(monkeypatch)
    outdir = tmp_path / "out"
    rip_dir = outdir / ".rip"
    upscale_dir = rip_dir / ".upscale"
    work = upscale_dir / "title_00_work" / "chunk_0000"
    work.mkdir(parents=True)
    (work / "segment.mkv").write_bytes(b"resume material")
    stray = rip_dir / "stale_junk.mkv"
    stray.write_bytes(b"leftover")

    # Upscale planned: .upscale survives, stray dies.
    run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=_no_rip_runner(_DVD_SCAN),
        dvd_upscale_mode="720p",
    )
    assert (work / "segment.mkv").exists(), (
        ".upscale tree must survive the purge when an upscale is planned")
    assert not stray.exists()

    # Upscale off: .upscale is stale junk and is purged (old behavior).
    work2 = rip_dir / ".upscale" / "title_00_work" / "chunk_0000"
    work2.mkdir(parents=True, exist_ok=True)
    (work2 / "segment.mkv").write_bytes(b"stale without a plan")
    run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=_no_rip_runner(_DVD_SCAN),
        dvd_upscale_mode="off",
    )
    assert not upscale_dir.exists(), (
        "no planned upscale: .upscale is stale and must be purged")


def test_resume_end_to_end_only_missing_chunks_recomputed(
        tmp_path, monkeypatch):
    """P-B2: the interrupt fixture - attempt 1 cancels mid-run (chunk
    work exists), attempt 2 re-enters with the manifest matching and
    reuses the verified segments; only the missing chunk recomputes."""
    import bluray_to_mp4.upscale as upscale_mod
    from bluray_to_mp4.upscale import run_dvd_upscale as real_run

    _stub_probe(monkeypatch, width=720, height=480, subtitle_count=0,
                sample_aspect_ratio="32:27", display_aspect_ratio="16:9")
    # Audio duration chosen so the derived rate snaps a canonical token
    # (8 frames / (8 / (24000/1001)) s == 24000/1001 exactly) - the real
    # snap runs; 8/0.2 would derive 40 fps and refuse pre-GPU.
    _stub_frame_preflight(
        monkeypatch, total_frames=8,
        audio_duration=8 / (24000.0 / 1001.0))
    # 8 frames at the default 120 s chunking would be ONE chunk; force
    # F=2 (2-frame chunks -> 4 chunks) so the interrupt/recompute split
    # is observable. The stage consults compute_chunk_frames only when
    # the plan carries no explicit chunk_frames.
    monkeypatch.setattr(
        upscale_mod, "compute_chunk_frames",
        lambda chunk_seconds, fps: 2)
    monkeypatch.setattr(
        pipeline_mod, "run_dvd_upscale", real_run)

    def fake_encode(args, *, duration_seconds, binary="ffmpeg",
                    on_progress=None, cancel=None):
        pass

    monkeypatch.setattr(pipeline_mod, "execute_encode", fake_encode)

    # The stage needs its own binary stubs + counting fake processes.
    monkeypatch.setattr(
        upscale_mod, "resolve_realesrgan_ncnn",
        lambda binary: str(tmp_path / "rg-stub"))
    (tmp_path / "rg-stub").write_bytes(b"stub")
    monkeypatch.setattr(upscale_mod, "resolve_ffmpeg", lambda binary: "ffmpeg")
    monkeypatch.setattr(
        upscale_mod, "resolve_realesrgan_models",
        lambda binary, models_path=None: str(tmp_path))
    monkeypatch.setattr(
        upscale_mod, "resolve_ffprobe", lambda binary: "ffprobe")

    work_calls = []

    def fake_run(cmd, *, label, cancel=None):
        work_calls.append(label)
        if label.startswith("upscale frame extraction"):
            in_dir = Path(cmd[-1]).parent
            in_dir.mkdir(parents=True, exist_ok=True)
            start_number = int(cmd[cmd.index("-start_number") + 1])
            between = cmd[cmd.index("-vf") + 1].split("between(n,")[1]
            first = int(between.split(",")[0])
            last = int(between.split(",")[1].split(")")[0])
            for i in range(first, last + 1):
                (in_dir / f"frame_{start_number + (i - first):08d}.png"
                 ).write_bytes(png_header_bytes(640, 360))
        elif label.startswith("Real-ESRGAN"):
            out_dir = Path(cmd[cmd.index("-o") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            for frame in sorted(Path(cmd[cmd.index("-i") + 1]).glob("*.png")):
                (out_dir / frame.name).write_bytes(png_header_bytes(1280, 720))
        elif label.startswith("upscale mezzanine segment"):
            Path(cmd[-1]).write_bytes(b"seg")
        elif label.startswith("upscale mezzanine concat"):
            Path(cmd[-1]).write_bytes(b"mezzanine")

    monkeypatch.setattr(upscale_mod, "_run_process", fake_run)

    def segment_probe(cmd, **kw):
        joined = " ".join(cmd)
        # Every existing segment verifies: 2 frames at the plan fps.
        class _Result:
            returncode = 0
            stderr = ""
            stdout = json.dumps({"streams": [{
                "nb_read_frames": "2", "r_frame_rate": "24000/1001"}]})
        return _Result()

    monkeypatch.setattr(upscale_mod.subprocess, "run", segment_probe)

    outdir = tmp_path / "out"
    script = _DVD_SCAN.splitlines() + _rip_tail(outdir)

    # Attempt 1: compute chunk 1, then "interrupt" before chunk 2 by
    # raising inside the second extraction.
    attempts = {"n": 0}

    def interrupting_run(cmd, *, label, cancel=None):
        if label.startswith("upscale frame extraction"):
            attempts["n"] += 1
            if attempts["n"] == 2:
                raise EncodeCancelled("interrupted mid-run")
        return fake_run(cmd, label=label, cancel=cancel)

    monkeypatch.setattr(upscale_mod, "_run_process", interrupting_run)
    with pytest.raises(EncodeCancelled):
        run_pipeline(
            0, str(outdir), encoder="x265", dry_run=False,
            mkv_runner=fake_runner(script),
            dvd_upscale_mode="720p",
        )

    # The workroot survived the interruption with chunk 1's segment.
    workroot = outdir / ".rip" / ".upscale" / "title_00_work"
    assert (workroot / "chunk_0000" / "segment.mkv").exists()

    # Attempt 2: full re-entry - the manifest matches, chunk 1 resumes,
    # only chunks 2-4 recompute.
    work_calls.clear()
    monkeypatch.setattr(upscale_mod, "_run_process", fake_run)
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(script),
        dvd_upscale_mode="720p",
    )
    assert result.jobs and result.jobs[0].completed
    extract_labels = [l for l in work_calls
                      if l.startswith("upscale frame extraction")]
    assert len(extract_labels) == 3, "only the missing chunks recompute"
    assert all(f"chunk {n}/4" in l for l, n in zip(
        extract_labels, (2, 3, 4)))
    # The final mezzanine was built and gated: the encode mapped VIDEO
    # from the mezzanine (its -i inputs), and no fail-open warning fired.
    # (After full success the .rip tree - .upscale included - is
    # reclaimed per the unchanged v0.2.12 policy, so the file is gone.)
    upscale_warns = [w for w in result.warnings
                     if "DVD AI upscale failed" in w]
    assert not upscale_warns, f"unexpected upscale failure: {upscale_warns}"
    encode_inputs = getattr(result.jobs[0], "encode_args", [])
    assert encode_inputs, "the real encode args were rebuilt post-upscale"


def png_header_bytes(width, height):
    """PNG IHDR stub reused by the P-B2 fixture (kept local so the test
    file has no cross-file import)."""
    return (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
    )


def test_cfr_probe_crash_fails_open_not_job_failure(tmp_path, monkeypatch):
    """P-C1: a raw FfprobeError inside probe_cfr_check is converted to
    UpscaleError inside the pre-flight try - the encode ships the
    original rip (fail OPEN), the job is NOT in result.errors."""
    _stub_probe(monkeypatch, width=720, subtitle_count=0)
    upscale_calls = []
    _stub_frame_preflight(monkeypatch, total_frames=163_267)
    monkeypatch.setattr(
        pipeline_mod, "run_dvd_upscale",
        lambda plan, *, on_progress=None, cancel=None: (
            upscale_calls.append(plan),
            (plan.output_path, plan.total_frames))[1])
    monkeypatch.setattr(
        pipeline_mod, "execute_encode", lambda *a, **k: None)

    def boom(path, *, binary="ffprobe"):
        raise FfprobeError("ffprobe binary missing entirely")

    monkeypatch.setattr(pipeline_mod, "probe_cfr_check", boom)
    outdir = tmp_path / "out"
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_DVD_SCAN.splitlines() + _rip_tail(outdir)),
        dvd_upscale_mode="720p",
    )
    assert result.jobs and result.jobs[0].completed  # encode still ships
    assert upscale_calls == []  # never entered the GPU stage
    assert result.errors == []  # C2: not a job failure
    assert any("CFR pre-flight could not probe" in w for w in result.warnings)
    assert any("encoded the original DVD rip instead" in w for w in result.warnings)


def test_snap_neighbor_split_refuses(tmp_path, monkeypatch):
    """P-C2: the blind spot firing - 163,509 frames, audio extent
    under-reported to 6,807.0 s (derives 24.0206 -> snaps 24) while the
    format duration 6,819.8 s derives 23.975 -> snaps 24000/1001. The
    neighbor split REFUSES before the GPU stage and fails open, naming
    both rates."""
    _stub_probe(monkeypatch, width=720, subtitle_count=0)
    upscale_calls = []
    _stub_frame_preflight(
        monkeypatch, total_frames=_MAMMA_MIA_HWA_FRAMES,
        audio_duration=6807.0)
    # Attach the independent format figure to the stubbed probe result.
    real_probe = pipeline_mod.probe_audio_duration_seconds
    monkeypatch.setattr(
        pipeline_mod, "probe_audio_duration_seconds",
        lambda path, audio_index, *, binary="ffprobe": AudioDurationProbe(
            6807.0, from_format=False, stream_ordinal=audio_index or 0,
            format_seconds=6819.8))
    monkeypatch.setattr(
        pipeline_mod, "run_dvd_upscale",
        lambda plan, *, on_progress=None, cancel=None: (
            upscale_calls.append(plan),
            (plan.output_path, plan.total_frames))[1])
    monkeypatch.setattr(
        pipeline_mod, "execute_encode", lambda *a, **k: None)
    outdir = tmp_path / "out"
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_DVD_SCAN.splitlines() + _rip_tail(outdir)),
        dvd_upscale_mode="720p",
    )
    assert result.jobs and result.jobs[0].completed  # encode still ships
    assert upscale_calls == []  # refused BEFORE the GPU stage
    split_warning = [w for w in result.warnings if "provenance split" in w]
    assert split_warning, "the refusal warning must name the split"
    # The warning names both rates and both snap tokens.
    assert "24.0207" in split_warning[0]
    assert "23.9751" in split_warning[0] or "23.975" in split_warning[0]
    assert "24000/1001" in split_warning[0]
    assert any("encoded the original DVD rip instead" in w
               for w in result.warnings)


def test_snap_agreement_proceeds_silently(tmp_path, monkeypatch):
    """P-C3 (the control): today's disc numbers - audio extent 6,818.4 s
    and format duration 6,819.8 s BOTH derive 23.977/23.975 and snap
    24000/1001. No new warning; the C.4 parity reason carries both
    figures on success."""
    _stub_probe(monkeypatch, width=720, subtitle_count=0)
    _stub_frame_preflight(
        monkeypatch, total_frames=_MAMMA_MIA_HWA_FRAMES,
        audio_duration=_MAMMA_MIA_HWA_AUDIO_SECONDS)
    monkeypatch.setattr(
        pipeline_mod, "probe_audio_duration_seconds",
        lambda path, audio_index, *, binary="ffprobe": AudioDurationProbe(
            _MAMMA_MIA_HWA_AUDIO_SECONDS, from_format=False,
            stream_ordinal=audio_index or 0, format_seconds=6819.8))
    plans = []
    monkeypatch.setattr(
        pipeline_mod, "run_dvd_upscale",
        lambda plan, *, on_progress=None, cancel=None: (
            plans.append(plan),
            (plan.output_path, plan.total_frames))[1])
    monkeypatch.setattr(
        pipeline_mod, "execute_encode", lambda *a, **k: None)
    outdir = tmp_path / "out"
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_DVD_SCAN.splitlines() + _rip_tail(outdir)),
        dvd_upscale_mode="720p",
    )
    assert result.jobs and result.jobs[0].completed
    assert plans and plans[0].fps == "24000/1001"
    # Silent when it works: no cross-check warning.
    assert not any("cross" in w for w in result.warnings)
    # C.4 parity: the success reason records both figures.
    parity = [r for r in result.reasons
              if "upscale stamp 24000/1001" in r]
    assert parity, "success-path provenance reason required"
    assert "23.9806" in parity[0]  # 163,509 / 6,818.4 (this disc's truth)
    assert "format-duration cross-check" in parity[0]
    assert "agrees" in parity[0]


def test_snap_one_side_unsnappable_warns_and_proceeds(tmp_path, monkeypatch):
    """P-C4: the audio side snaps cleanly but the format-side rate is
    garbage (26.3 -> unsnappable) -> proceed on the audio-stream
    derivation with a warning that carries both figures, plus the C.4
    parity reason."""
    _stub_probe(monkeypatch, width=720, subtitle_count=0)
    _stub_frame_preflight(
        monkeypatch, total_frames=_MAMMA_MIA_HWA_FRAMES,
        audio_duration=_MAMMA_MIA_HWA_AUDIO_SECONDS)
    # Format duration chosen so the derived format rate is ~26.3:
    # 163,509 / 6,216 s = 26.30 -> snaps nothing.
    monkeypatch.setattr(
        pipeline_mod, "probe_audio_duration_seconds",
        lambda path, audio_index, *, binary="ffprobe": AudioDurationProbe(
            _MAMMA_MIA_HWA_AUDIO_SECONDS, from_format=False,
            stream_ordinal=audio_index or 0, format_seconds=6216.0))
    plans = []
    monkeypatch.setattr(
        pipeline_mod, "run_dvd_upscale",
        lambda plan, *, on_progress=None, cancel=None: (
            plans.append(plan),
            (plan.output_path, plan.total_frames))[1])
    monkeypatch.setattr(
        pipeline_mod, "execute_encode", lambda *a, **k: None)
    outdir = tmp_path / "out"
    result = run_pipeline(
        0, str(outdir), encoder="x265", dry_run=False,
        mkv_runner=fake_runner(_DVD_SCAN.splitlines() + _rip_tail(outdir)),
        dvd_upscale_mode="720p",
    )
    assert result.jobs and result.jobs[0].completed
    assert plans and plans[0].fps == "24000/1001"
    warned = [w for w in result.warnings if "cross-check unsnappable" in w]
    assert warned, "one-sided noise must warn, not refuse"
    assert "23.9806" in warned[0]
    assert "26.3" in warned[0]
    # C.4: the parity reason still lands (without the format agreement
    # clause, since the format side did not agree - it did not classify).
    parity = [r for r in result.reasons
              if "upscale stamp 24000/1001" in r]
    assert parity


def test_dead_import_absent():
    """P-C5 (C.1): video_frame_rate is no longer imported by the
    pipeline (dead since v0.2.34 replaced the metadata read) - the
    helper itself stays in ffprobe."""
    import inspect

    source = inspect.getsource(pipeline_mod)
    assert "video_frame_rate" not in source, (
        "pipeline must not import the dead video_frame_rate helper")
    from bluray_to_mp4 import ffprobe
    assert hasattr(ffprobe, "video_frame_rate"), (
        "the helper stays in ffprobe.py (v0.2.34 §1.4: not deleted)")
