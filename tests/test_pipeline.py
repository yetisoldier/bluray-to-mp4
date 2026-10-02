"""End-to-end pipeline dry-run tests (no makemkvcon/ffmpeg required)."""

import os
import sys

import pytest

from bluray_to_mp4 import pipeline as pipeline_mod
from bluray_to_mp4.cli import build_parser
from bluray_to_mp4.ffprobe import (
    ProbedStream,
    is_interlaced,
    nth_of_type,
    parse_probe,
)
from bluray_to_mp4.makemkv import _CancelState, make_runner
from bluray_to_mp4.pipeline import EncodeCancelled, execute_encode, run_pipeline
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
    assert any("roadmap #2" in w for w in result.warnings)


def test_movie_dry_run_no_subtitles_skips_srt(tmp_path):
    """want_subtitles=False: no srt_path, no OCR reservation warning."""
    result = _movie_run(tmp_path / "out", want_subtitles=False)
    assert result.jobs, "movie dry run must still plan the encode"
    for job in result.jobs:
        assert job.srt_path is None
        assert job.subtitle_stream_index is None
    assert not any("roadmap #2" in w for w in result.warnings)
    assert not any("SRT path reserved" in w for w in result.warnings)


def test_series_dry_run_no_subtitles_skips_srt(tmp_path):
    """Same contract for every episode of a series disc."""
    result = _series_run(tmp_path / "out", want_subtitles=False)
    assert result.is_series and len(result.jobs) == 4
    assert all(job.srt_path is None for job in result.jobs)
    assert not any("roadmap #2" in w for w in result.warnings)


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


def _stub_probe(monkeypatch):
    monkeypatch.setattr(
        pipeline_mod, "probe_streams",
        lambda path, *, binary="ffprobe": [
            ProbedStream(0, "video", "h264"),
            ProbedStream(1, "audio", "ac3", channels=6),
        ],
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
        tid = args[3]  # "-r mkv disc:0 <tid> <dir> --minlength=..."
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
