"""TV-series detection and episode naming.

MakeMKV reports every episode as its own title with a duration, so a
series disc shows a cluster of similarly-sized long titles while a movie
disc shows one dominant title plus short extras.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Disc, Title

__all__ = ["SeriesGuess", "detect_series", "episode_output_names", "main_feature"]

MIN_EPISODE_SECONDS = 600  # 10 minutes; shorter titles are extras/promos
DOMINANCE_RATIO = 2.2  # main title >= 2.2x the next-longest title -> movie


@dataclass
class SeriesGuess:
    """Whether a disc looks like a series, and which titles are episodes."""

    is_series: bool
    episode_tids: list[int] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def episode_count(self) -> int:
        return len(self.episode_tids)


def _candidates(disc: Disc, *, min_episode: int) -> list[Title]:
    return [t for t in disc.titles if t.duration_seconds >= min_episode]


def detect_series(disc: Disc, *, min_episode: int = MIN_EPISODE_SECONDS) -> SeriesGuess:
    """Classify a disc as series vs movie based on title durations."""
    cands = sorted(_candidates(disc, min_episode=min_episode),
                   key=lambda t: t.duration_seconds, reverse=True)
    if len(cands) < 2:
        if cands:
            return SeriesGuess(False, [], [
                f"Single long title ({cands[0].duration_seconds // 60} min) - movie disc"
            ])
        return SeriesGuess(False, [], ["No titles long enough to classify"])

    top, second = cands[0], cands[1]
    if top.duration_seconds >= DOMINANCE_RATIO * second.duration_seconds:
        return SeriesGuess(False, [], [
            f"Main title is {top.duration_seconds // 60} min vs "
            f"{second.duration_seconds // 60} min for the next-longest - movie disc"
        ])

    episodes = sorted(cands, key=lambda t: t.tid)
    durs = [t.duration_seconds // 60 for t in episodes]
    return SeriesGuess(True, [t.tid for t in episodes], [
        f"{len(episodes)} similar-length titles "
        f"({', '.join(str(d) for d in durs[:4])}{'...' if len(durs) > 4 else ''} min) - series disc"
    ])


def main_feature(disc: Disc, *, min_episode: int = MIN_EPISODE_SECONDS) -> Title | None:
    """The main movie title: longest candidate, ties broken by chapter count."""
    cands = _candidates(disc, min_episode=min_episode)
    if not cands:
        return None
    return max(cands, key=lambda t: (t.duration_seconds, t.chapters, -t.tid))


def episode_output_names(
    series_name: str,
    season: int,
    *,
    start_episode: int = 1,
    episode_tids: list[int],
    episode_titles: dict[int, str] | None = None,
    base_names: dict[int, str] | None = None,
    title_overrides: dict[int, str] | None = None,
) -> list[tuple[int, str]]:
    """Build (tid, output filename) pairs in episode order.

    Format: "Series - S01E05 - Episode Title.mp4" (Plex/Jellyfin standard).
    Priority per episode: explicit user edit (title_overrides) > fetched
    title (episode_titles) > disc-derived base name > numbered fallback.
    """
    from .naming import sanitize_filename

    episode_titles = episode_titles or {}
    title_overrides = title_overrides or {}
    base_names = base_names or {}
    out: list[tuple[int, str]] = []
    n = start_episode
    for tid in episode_tids:
        label = (
            title_overrides.get(tid)
            or episode_titles.get(tid)
            or base_names.get(tid)
            or f"Episode {n}"
        )
        fname = f"{sanitize_filename(series_name)} - S{season:02d}E{n:02d} - {sanitize_filename(label)}.mp4"
        out.append((tid, fname))
        n += 1
    return out