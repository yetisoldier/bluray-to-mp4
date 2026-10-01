"""End-to-end pipeline dry-run tests (no makemkvcon/ffmpeg required)."""

import pytest

from bluray_to_mp4.cli import build_parser
from bluray_to_mp4.ffprobe import is_interlaced, nth_of_type, parse_probe
from bluray_to_mp4.pipeline import run_pipeline
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