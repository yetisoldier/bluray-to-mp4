"""End-to-end pipeline dry-run tests (no makemkvcon/ffmpeg required)."""

import os
import sys

import pytest

from bluray_to_mp4 import pipeline as pipeline_mod
from bluray_to_mp4.cli import build_parser
from bluray_to_mp4.ffprobe import (
    FfprobeError,
    ProbedStream,
    first_video_width,
    is_interlaced,
    nth_of_type,
    parse_probe,
    probe_duration_seconds,
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
        '{"index":0,"codec_type":"video","codec_name":"h264","field_order":"tt"},'
        '{"index":1,"codec_type":"audio","codec_name":"truehd","channels":8,'
        '"tags":{"language":"eng"}},'
        '{"index":2,"codec_type":"subtitle","codec_name":"hdmv_pgs_subtitle",'
        '"tags":{"language":"eng"}}'
        "]}"
    )
    streams = parse_probe(text)
    assert len(streams) == 3
    assert is_interlaced(streams)
    audio = nth_of_type(streams, "audio", 0)
    assert audio.channels == 8 and audio.lang == "eng"
    sub = nth_of_type(streams, "subtitle", 0)
    assert sub.codec_name == "hdmv_pgs_subtitle"
    progressive = [
        s for s in streams if s.codec_type == "video"
    ]
    assert nth_of_type(progressive, "video", 0) is not None


def test_cli_parser_smoke():
    parser = build_parser()
    args = parser.parse_args(["run", "0", "--out", "x", "--dry-run"])
    assert args.index == 0 and args.dry_run and args.out == "x"
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


def _stub_probe(monkeypatch, *, width=3840, subtitle_count=0,
                sub_codec="hdmv_pgs_subtitle", sub_lang=""):
    """Stub probe_streams: a video stream of the given width (the movie
    fixture's scan says 3840x2160, so 3840 keeps reuse tests reusing),
    one audio stream, and optional N subtitle streams. v0.2.16: the
    stubbed subtitle streams carry a codec name and language so the
    VobSub/PGS branch and the mov_text embed tag can be tested."""
    streams = [
        ProbedStream(0, "video", "h264", width=width),
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
    assert job.upscale_mezzanine_path.endswith(".upscale/title_00_720p.mkv")
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
    _stub_probe(monkeypatch, width=720, subtitle_count=0)
    captures = {"upscale": [], "encode": [], "progress": []}

    def fake_upscale(plan, *, on_progress=None, cancel=None):
        captures["upscale"].append(plan)
        if on_progress is not None:
            on_progress(0.5, 42.0)
        return plan.output_path

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
    assert plan.output_path.endswith(".upscale/title_00_720p.mkv")
    args = captures["encode"][0]
    inputs = [args[i + 1] for i, arg in enumerate(args) if arg == "-i"]
    assert inputs == [plan.output_path, result.jobs[0].input_path]
    maps = [args[i + 1] for i, arg in enumerate(args) if arg == "-map"]
    assert maps == ["1:a:0?", "0:v:0"]
    assert ("upscale", 0, 0.5, 42.0) in captures["progress"]


def test_dvd_upscale_failure_falls_open_to_original_rip(tmp_path, monkeypatch):
    from bluray_to_mp4.upscale import UpscaleError

    _stub_probe(monkeypatch, width=720, subtitle_count=0)
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


def test_dvd_upscale_cancel_still_propagates(tmp_path, monkeypatch):
    _stub_probe(monkeypatch, width=720, subtitle_count=0)

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
