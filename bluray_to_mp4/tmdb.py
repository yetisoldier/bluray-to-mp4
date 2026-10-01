"""TMDB lookups: episode titles and movie years (optional, free API key).

Everything here is stdlib-only (urllib) and every call site takes an
injectable fetcher, so the lookup logic is fully testable offline.
Without an API key the feature is simply unavailable - callers degrade
to manual naming and a warning, never a hard failure.

Free key: https://www.themoviedb.org/settings/api (register, request a
"Developer" key, copy the v3 API key value).
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from collections.abc import Callable

__all__ = [
    "TmdbError",
    "TmdbKeyMissing",
    "api_key_from",
    "search_show",
    "season_episodes",
    "season_episode_titles",
    "lookup_movie",
]

API_BASE = "https://api.themoviedb.org/3"
KEY_HELP = (
    "No TMDB API key set. Get a free one at "
    "https://www.themoviedb.org/settings/api (Developer key), then paste "
    "it in the app's TMDB field or set the B2M_TMDB_KEY environment "
    "variable."
)

Fetcher = Callable[[str], str]


class TmdbError(RuntimeError):
    """TMDB lookup failed."""


class TmdbKeyMissing(TmdbError):
    """No API key configured; the feature is disabled."""


def api_key_from(*, config_key: str = "", env_var: str = "B2M_TMDB_KEY") -> str:
    """Resolve the API key: explicit value, then environment."""
    key = (config_key or "").strip() or (os.environ.get(env_var, "") or "").strip()
    if not key:
        raise TmdbKeyMissing(KEY_HELP)
    return key


def _get_json(
    path: str,
    params: dict,
    *,
    key: str = "",
    fetcher: Fetcher | None = None,
    timeout: float = 15.0,
) -> dict:
    qs = urllib.parse.urlencode({**params, "api_key": api_key_from(config_key=key)})
    url = f"{API_BASE}{path}?{qs}"
    if fetcher is None:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise TmdbError(f"TMDB returned HTTP {exc.code} for {path}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise TmdbError(f"TMDB unreachable: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise TmdbError(f"TMDB sent unparsable JSON: {exc}") from exc
    try:
        return json.loads(fetcher(url))
    except json.JSONDecodeError as exc:
        raise TmdbError(f"TMDB sent unparsable JSON: {exc}") from exc


def search_show(name: str, *, key: str = "", fetcher: Fetcher | None = None) -> dict | None:
    """First TV search hit: {'id', 'name', 'first_air_date'} or None."""
    data = _get_json("/search/tv", {"query": name, "language": "en-US"},
                     key=key, fetcher=fetcher)
    results = data.get("results") or []
    if not results:
        return None
    first = results[0]
    return {
        "id": first.get("id"),
        "name": first.get("name") or first.get("original_name") or name,
        "first_air_date": first.get("first_air_date", ""),
    }


def season_episodes(
    show_name: str,
    season: int,
    *,
    key: str = "",
    fetcher: Fetcher | None = None,
) -> list[dict] | None:
    """Episodes of a season: [{'episode_number', 'name'}, ...] or None."""
    show = search_show(show_name, key=key, fetcher=fetcher)
    if show is None or not show.get("id"):
        return None
    try:
        data = _get_json(
            f"/tv/{show['id']}/season/{season}",
            {"language": "en-US"}, key=key, fetcher=fetcher,
        )
    except TmdbError:
        # Unknown season numbers 404; treat as "not found", not a crash.
        return None
    episodes = data.get("episodes") or []
    return [
        {"episode_number": e.get("episode_number"), "name": e.get("name") or ""}
        for e in episodes
    ]


def season_episode_titles(
    show_name: str,
    season: int,
    *,
    start_episode: int = 1,
    count: int | None = None,
    key: str = "",
    fetcher: Fetcher | None = None,
) -> list[str | None]:
    """Episode titles for absolute numbers start_episode..start_episode+count.

    Returns a list aligned 1:1 with the requested episode numbers; entries
    are the TMDB title, or None when the episode (or its title) is unknown.
    """
    episodes = season_episodes(show_name, season, key=key, fetcher=fetcher)
    if episodes is None:
        return [None] * (count or 0)
    by_number = {e["episode_number"]: e["name"] for e in episodes}
    numbers = [start_episode + i for i in range(count or 0)]
    return [by_number.get(n) or None for n in numbers]


def lookup_movie(
    title: str,
    *,
    key: str = "",
    fetcher: Fetcher | None = None,
) -> dict | None:
    """First movie search hit: {'title', 'year'} or None."""
    data = _get_json("/search/movie", {"query": title, "language": "en-US"},
                     key=key, fetcher=fetcher)
    results = data.get("results") or []
    if not results:
        return None
    first = results[0]
    release = first.get("release_date") or ""
    year = int(release[:4]) if len(release) >= 4 and release[:4].isdigit() else None
    return {"title": first.get("title") or title, "year": year}