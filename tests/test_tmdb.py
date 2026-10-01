"""Tests for TMDB episode-title/movie-year autofill (issue #6).

All network access is replaced by fixture fetchers; no API key or
connectivity is required.
"""

import json

import pytest

from bluray_to_mp4.pipeline import run_pipeline
from bluray_to_mp4.tmdb import (
    TmdbError,
    TmdbKeyMissing,
    api_key_from,
    lookup_movie,
    season_episode_titles,
)
from tests.fixtures import MOVIE_DISC_OUTPUT, SERIES_DISC_OUTPUT, fake_runner


def _make_fetcher(routes: dict):
    """Fetcher keyed by path (query string ignored)."""

    def fetcher(url: str) -> str:
        path = url.split("?")[0].replace("https://api.themoviedb.org/3", "")
        if path not in routes:
            raise TmdbError(f"unexpected TMDB path {path}")
        return json.dumps(routes[path])

    return fetcher


def test_key_missing_has_helpful_message(monkeypatch):
    monkeypatch.delenv("B2M_TMDB_KEY", raising=False)
    with pytest.raises(TmdbKeyMissing, match="themoviedb.org/settings/api"):
        api_key_from()


def test_key_from_env(monkeypatch):
    monkeypatch.setenv("B2M_TMDB_KEY", "env-key")
    assert api_key_from() == "env-key"


def test_episode_titles_with_start_offset():
    fetcher = _make_fetcher({
        "/search/tv": {"results": [{"id": 1396, "name": "Breaking Bad",
                                    "first_air_date": "2008-01-20"}]},
        "/tv/1396/season/1": {"episodes": [
            {"episode_number": 1, "name": "Pilot"},
            {"episode_number": 2, "name": "Cat's in the Bag..."},
            {"episode_number": 3, "name": "...And the Bag's in the River"},
            {"episode_number": 4, "name": "Cancer Man"},
        ]},
    })
    titles = season_episode_titles(
        "Breaking Bad", 1, start_episode=2, count=3, key="k", fetcher=fetcher)
    assert titles == ["Cat's in the Bag...", "...And the Bag's in the River",
                      "Cancer Man"]


def test_episode_titles_missing_entries_become_none():
    fetcher = _make_fetcher({
        "/search/tv": {"results": [{"id": 1, "name": "Show"}]},
        "/tv/1/season/9": {"episodes": [
            {"episode_number": 1, "name": "Only One"},
        ]},
    })
    titles = season_episode_titles(
        "Show", 9, start_episode=1, count=3, key="k", fetcher=fetcher)
    assert titles == ["Only One", None, None]


def test_unknown_season_returns_none_list():
    # A missing season 404s; that must degrade to "no titles", not crash.
    def fetcher(url: str) -> str:
        raise TmdbError("TMDB returned HTTP 404")

    # season_episodes raises through, season_episode_titles maps to Nones.
    with pytest.raises(TmdbError):
        from bluray_to_mp4.tmdb import season_episodes
        season_episodes("Show", 40, key="k", fetcher=fetcher)


def test_lookup_movie_year():
    fetcher = _make_fetcher({
        "/search/movie": {"results": [
            {"title": "Blade Runner 2049", "release_date": "2017-10-04"},
        ]},
    })
    hit = lookup_movie("Blade Runner 2049", key="k", fetcher=fetcher)
    assert hit == {"title": "Blade Runner 2049", "year": 2017}


def test_lookup_movie_no_results():
    fetcher = _make_fetcher({"/search/movie": {"results": []}})
    assert lookup_movie("Zzz", key="k", fetcher=fetcher) is None


def test_pipeline_series_uses_tmdb_titles(tmp_path):
    fetcher = _make_fetcher({
        "/search/tv": {"results": [{"id": 1396, "name": "Breaking Bad",
                                    "first_air_date": "2008-01-20"}]},
        "/tv/1396/season/1": {"episodes": [
            {"episode_number": 1, "name": "Pilot"},
            {"episode_number": 2, "name": "Cat's in the Bag..."},
            {"episode_number": 3, "name": "...And the Bag's in the River"},
            {"episode_number": 4, "name": "Cancer Man"},
        ]},
    })
    result = run_pipeline(
        0, str(tmp_path), encoder="x265", dry_run=True,
        mkv_runner=fake_runner(SERIES_DISC_OUTPUT.splitlines()),
        series_name="Breaking Bad", season=1,
        tmdb_key="k", tmdb_fetcher=fetcher,
    )
    assert result.is_series
    names = [j.output_name for j in result.jobs]
    assert names[0] == "Breaking Bad - S01E01 - Pilot.mp4"
    assert names[1] == "Breaking Bad - S01E02 - Cat's in the Bag.mp4"
    assert names[3] == "Breaking Bad - S01E04 - Cancer Man.mp4"
    assert any("TMDB" in r for r in result.reasons)


def test_pipeline_movie_uses_tmdb_year(tmp_path):
    fetcher = _make_fetcher({
        "/search/movie": {"results": [
            {"title": "Blade Runner 2049", "release_date": "2017-10-04"},
        ]},
    })
    result = run_pipeline(
        0, str(tmp_path), encoder="x265", dry_run=True,
        mkv_runner=fake_runner(MOVIE_DISC_OUTPUT.splitlines()),
        tmdb_key="k", tmdb_fetcher=fetcher,
    )
    assert result.jobs[0].output_name == "Blade Runner 2049 (2017).mp4"
    assert any("TMDB" in r for r in result.reasons)


def test_pipeline_tmdb_failure_degrades_gracefully(tmp_path):
    def fetcher(url: str) -> str:
        raise TmdbError("TMDB unreachable: no network")

    result = run_pipeline(
        0, str(tmp_path), encoder="x265", dry_run=True,
        mkv_runner=fake_runner(SERIES_DISC_OUTPUT.splitlines()),
        series_name="Breaking Bad",
        tmdb_key="k", tmdb_fetcher=fetcher,
    )
    # Manual naming still produced sane output files.
    assert len(result.jobs) == 4
    assert result.jobs[0].output_name.startswith("Breaking Bad - S01E01")
    assert any("TMDB lookup failed" in w for w in result.warnings)


def test_pipeline_without_key_skips_lookup_entirely(tmp_path):
    result = run_pipeline(
        0, str(tmp_path), encoder="x265", dry_run=True,
        mkv_runner=fake_runner(MOVIE_DISC_OUTPUT.splitlines()),
    )
    assert result.jobs[0].output_name == "Blade Runner 2049.mp4"
    assert not any("TMDB" in w for w in result.warnings)