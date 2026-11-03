"""Tests for worker-layer warning synthesis (no Qt event loop needed).

v0.2.37 (L1): _job_warnings must stop synthesizing a false
"subtitle missing (unknown reason)" for deliberately subtitle-less
runs (the "Include subtitles" toggle off, or an explicit
"(no subtitles)" Review-page pick), while keeping the killed-OCR-child
honesty for runs that wanted subtitles and got none.
"""

from types import SimpleNamespace

from bluray_to_mp4.ui.workers import _job_warnings


def _job(**kw):
    """A completed job-shaped SimpleNamespace (pipeline TitleJob duck)."""
    defaults = dict(
        output_name="Episode.mp4",
        srt_path=None,
        selection=None,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _result(*warnings):
    return SimpleNamespace(warnings=list(warnings))


def test_subtitles_off_no_srt_no_warnings():
    """want_subtitles=False (the toggle off): a completed job with no
    sidecar and no pipeline warnings carries no synthesized warning -
    intended behavior is not a warning."""
    job = _job()
    assert _job_warnings(_result(), job, want_subtitles=False) == []


def test_user_declined_subtitles_no_warning():
    """Karen's EXP-2 shape: an explicit "(no subtitles)" Review-page
    pick (selection.subtitle_user_declined) is intended behavior -
    no synthesized warning even with subtitles wanted for the run."""
    job = _job(selection=SimpleNamespace(subtitle_user_declined=True))
    assert _job_warnings(_result(), job, want_subtitles=True) == []


def test_wanted_subtitles_missing_srt_synthesizes_warning():
    """Legacy honesty preserved: subtitles requested (default lane), no
    sidecar, no pipeline warning -> the synthesized warning still shows
    (the killed-OCR-child case)."""
    job = _job()
    assert _job_warnings(_result(), job) == [
        "subtitle missing (unknown reason)"
    ]
    assert _job_warnings(_result(), job, want_subtitles=True) == [
        "subtitle missing (unknown reason)"
    ]


def test_pipeline_warnings_pass_through_regardless():
    """Name-prefixed pipeline warnings (upscale fail-open, missing
    streams) return verbatim whether or not subtitles were wanted."""
    job = _job()
    result = _result(f"{job.output_name}: DVD AI upscale failed; "
                     "encoded the original DVD rip instead.")
    assert _job_warnings(result, job, want_subtitles=False) == [
        f"{job.output_name}: DVD AI upscale failed; "
        "encoded the original DVD rip instead."
    ]
    assert _job_warnings(result, job, want_subtitles=True) == [
        f"{job.output_name}: DVD AI upscale failed; "
        "encoded the original DVD rip instead."
    ]


def test_srt_path_short_circuits():
    """job.srt_path set -> [] (existing short-circuit preserved)."""
    job = _job(srt_path="/out/Episode.srt")
    assert _job_warnings(_result(), job, want_subtitles=True) == []
    assert _job_warnings(_result(), job, want_subtitles=False) == []