"""Novice-friendly track recommendations for a scanned title or disc.

The engine scores every audio/subtitle stream and returns one
recommended pick per category plus human-readable reasons, so the UI can
show "Recommended" badges and a one-click "use suggestions" button while
still letting users override each choice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import StreamInfo, Title

__all__ = ["Selection", "recommend_title", "recommend_disc"]

COMMENTARY_RE = re.compile(r"comment", re.I)
DESCRIPTIVE_RE = re.compile(r"descriptive|visual.?ly impaired|narrat", re.I)
SDH_RE = re.compile(r"\bSDH\b|songs? ?& ?|hard ?of ?hear|hearing impaired|cc\b", re.I)
FORCED_RE = re.compile(r"\bforced?\b", re.I)

# Codec quality tiers (higher = better).
_CODEC_TIERS = (
    (re.compile(r"truehd|atmos|dts-?hd|lpcm|pcm|master audio", re.I), 40),
    (re.compile(r"\bdts\b|e-?ac-?3|dolby digital|ac-?3", re.I), 30),
    (re.compile(r"aac|mp2|mpeg audio", re.I), 15),
)


def _text(*parts: str) -> str:
    return " ".join(p for p in parts if p)


def is_commentary(stream: StreamInfo) -> bool:
    text = _text(stream.name, stream.codec_long, stream.all_text())
    return bool(COMMENTARY_RE.search(text))


def is_descriptive(stream: StreamInfo) -> bool:
    text = _text(stream.name, stream.codec_long)
    return bool(DESCRIPTIVE_RE.search(text))


def _is_english(stream: StreamInfo) -> bool:
    return stream.lang_code in {"eng", "en"} or (
        stream.lang_name or "").strip().lower().startswith("english")


def _codec_tier(stream: StreamInfo) -> int:
    text = _text(stream.codec, stream.codec_long, stream.name)
    for pattern, score in _CODEC_TIERS:
        if pattern.search(text):
            return score
    return 0


def _classify_sub(stream: StreamInfo) -> str:
    text = _text(stream.name, stream.codec_long)
    if is_commentary(stream):
        return "commentary"
    if SDH_RE.search(text):
        return "sdh"
    if FORCED_RE.search(text):
        return "forced"
    return "normal"


def _score_audio(stream: StreamInfo, *, english_bonus: int = 100) -> tuple[int, list[str]]:
    reasons: list[str] = []
    score = 0
    if _is_english(stream):
        score += english_bonus
        reasons.append("English audio")
    if is_commentary(stream) or is_descriptive(stream):
        score -= 1000
        reasons.append("Commentary/descriptive track (skipped)")
    channels = stream.channels
    if channels:
        score += min(channels, 8) * 5
        reasons.append(f"{channels} channel{'s' if channels != 1 else ''}")
    tier = _codec_tier(stream)
    score += tier
    if stream.bitrate_kbps:
        score += min(stream.bitrate_kbps // 100, 20)
    if not reasons:
        reasons.append("Best available audio track")
    return score, reasons


def _score_sub(stream: StreamInfo) -> tuple[int, list[str]]:
    reasons: list[str] = []
    score = 0
    if _is_english(stream):
        score += 100
        reasons.append("English subtitles")
    if is_commentary(stream):
        score -= 1000
        return score, ["Commentary subtitles (skipped)"]
    if is_descriptive(stream):  # G1: descriptive narration subs are not standard
        score -= 500
        return score, ["Descriptive-narration subtitles (skipped)"]
    kind = _classify_sub(stream)
    if kind == "normal":
        score += 30
        reasons.append("Standard subtitles")
    elif kind == "sdh":
        score += 10
        reasons.append("SDH (includes sound effects)")
    elif kind == "forced":
        score += 5
        reasons.append("Forced subtitles (foreign dialogue only)")
    return score, reasons


@dataclass
class Selection:
    """What we suggest keeping for one title."""

    tid: int
    audio_sid: int | None = None
    subtitle_sid: int | None = None
    audio_reasons: list[str] = field(default_factory=list)
    subtitle_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # v0.2.15: user picked "(no subtitles)" on the Review page - the
    # rip-time first-stream fallback must stay off for this title.
    subtitle_user_declined: bool = False

    @property
    def has_english_audio(self) -> bool:
        return any("English audio" in r for r in self.audio_reasons)

    @property
    def has_english_subs(self) -> bool:
        return any("English subtitles" in r for r in self.subtitle_reasons)


def recommend_title(title: Title, *, want_subtitles: bool = True) -> Selection:
    """Pick the best audio/subtitle streams for one title."""
    sel = Selection(tid=title.tid)
    audios = title.streams_of("audio")
    subs = title.streams_of("subtitle")

    best_audio, best_reasons = None, []
    best_score = -10_000
    for s in audios:
        score, reasons = _score_audio(s)
        if score > best_score:
            best_audio, best_score, best_reasons = s, score, reasons
    if best_audio is not None:
        sel.audio_sid = best_audio.sid
        sel.audio_reasons = best_reasons
        # G2: a commentary-only disc must warn, not silently recommend
        # commentary as if it were the feature audio.
        if is_commentary(best_audio) or is_descriptive(best_audio):
            sel.warnings.append(
                "Only commentary/descriptive audio exists on this title; "
                "the movie's own audio may be missing - check manually."
            )
    elif audios:
        sel.warnings.append("No clean audio track found; review manually.")

    if want_subtitles:
        best_sub, best_sub_reasons = None, []
        best_sub_score = -10_000
        for s in subs:
            score, reasons = _score_sub(s)
            if score > best_sub_score:
                best_sub, best_sub_score, best_sub_reasons = s, score, reasons
        # v0.2.14 (Eric: English subs included by default, players can
        # toggle off): only a POSITIVE English match used to select here;
        # when the scan can't confirm the language (Mamma Mia's PGS
        # tracks), the Review page showed NOTHING to select. Now the best
        # non-commentary/descriptive subtitle is always selected; when
        # English is unconfirmed the reasons say so.
        if best_sub is not None and best_sub_score >= 0:
            sel.subtitle_sid = best_sub.sid
            sel.subtitle_reasons = best_sub_reasons
            if best_sub_score < 100:
                sel.subtitle_reasons = list(best_sub_reasons) + [
                    "English not confirmed by the disc scan; verify the "
                    "language before relying on it."]

    if not sel.has_english_audio and audios:
        sel.warnings.append(
            "No English audio track found; picked the best available track."
        )
    return sel


def recommend_disc(titles: list[Title]) -> list[Selection]:
    """Recommendations for every title on the disc (UI filters what to show)."""
    return [recommend_title(t) for t in titles]