"""Tests for series detection, episode naming, and filename sanitizing."""

from bluray_to_mp4.makemkv import scan_disc
from bluray_to_mp4.naming import movie_output_name, sanitize_filename, subtitle_sidecar
from bluray_to_mp4.series import detect_series, episode_output_names, main_feature
from tests.fixtures import MOVIE_DISC_OUTPUT, SERIES_DISC_OUTPUT, fake_runner


def _movie_disc():
    return scan_disc(0, runner=fake_runner(MOVIE_DISC_OUTPUT.splitlines()))


def _series_disc():
    return scan_disc(0, runner=fake_runner(SERIES_DISC_OUTPUT.splitlines()))


def test_movie_disc_not_series():
    disc = _movie_disc()
    guess = detect_series(disc)
    assert not guess.is_series
    assert guess.episode_tids == []
    assert any("movie disc" in r for r in guess.reasons)


def test_series_disc_detected():
    disc = _series_disc()
    guess = detect_series(disc)
    assert guess.is_series
    assert guess.episode_tids == [0, 1, 2, 3]  # 4-min extra excluded
    assert guess.episode_count == 4


def test_main_feature_is_longest_title():
    disc = _movie_disc()
    main = main_feature(disc)
    assert main is not None and main.tid == 0


def test_episode_names_plex_standard():
    names = episode_output_names(
        "Breaking Bad",
        1,
        episode_tids=[0, 1, 2, 3],
        episode_titles={0: "Pilot", 1: "Cat's in the Bag..."},
    )
    tids = [tid for tid, _ in names]
    assert tids == [0, 1, 2, 3]
    assert names[0][1] == "Breaking Bad - S01E01 - Pilot.mp4"
    # Untitled episodes fall back to numbered labels; trailing dots are
    # stripped because Windows filenames cannot end with a dot.
    assert names[1][1] == "Breaking Bad - S01E02 - Cat's in the Bag.mp4"
    assert names[2][1] == "Breaking Bad - S01E03 - Episode 3.mp4"


def test_episode_names_start_offset():
    names = episode_output_names(
        "Breaking Bad", 2, start_episode=5, episode_tids=[0, 1]
    )
    assert names[0][1] == "Breaking Bad - S02E05 - Episode 5.mp4"
    assert names[1][1] == "Breaking Bad - S02E06 - Episode 6.mp4"


def test_sanitize_removes_windows_illegal_chars():
    assert sanitize_filename('Movie: The "Reel" <Story>?') == "Movie The Reel Story"
    assert sanitize_filename("CON") == "_CON"
    assert sanitize_filename("  spaced   out. ") == "spaced out"


def test_movie_output_name():
    assert movie_output_name("Blade Runner 2049", year=2017) == "Blade Runner 2049 (2017).mp4"
    assert movie_output_name("Up") == "Up.mp4"


def test_subtitle_sidecar():
    assert subtitle_sidecar("Show - S01E01.mp4") == "Show - S01E01.srt"
    assert subtitle_sidecar("Movie (2019).MP4") == "Movie (2019).srt"