"""Core data model for disc contents parsed from MakeMKV robot output."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "Attribute",
    "StreamInfo",
    "Title",
    "Disc",
    "parse_duration",
    "parse_channel_count",
    "parse_bitrate_kbps",
]


def parse_duration(value: str) -> int:
    """Convert MakeMKV 'H:MM:SS' duration strings to seconds."""
    parts = value.strip().split(":")
    if len(parts) != 3:
        raise ValueError(f"unexpected duration value {value!r}")
    h, m, s = (int(p) for p in parts)
    return h * 3600 + m * 60 + s


def parse_channel_count(text: str) -> int:
    """Best-effort channel count from free-form strings.

    '6 channels' -> 6, '5.1' -> 6, '7.1 channels' -> 8, '3/2+1' -> 6.
    """
    if not text:
        return 0
    m = re.search(r"(\d+)\.(\d+)\s*ch(?:annels?)?\b", text, re.I)
    if m:  # '7.1 channels' -> 7 + LFE = 8
        return int(m.group(1)) + (1 if int(m.group(2)) else 0)
    m = re.search(r"\b(\d+)[\s/]+ch(?:annels?)?\b", text, re.I)
    if m:  # '6 channels' -> 6
        return int(m.group(1))
    m = re.search(r"\b(\d+)\.(\d+)\b", text)
    if m:  # bare '5.1' -> 6
        return int(m.group(1)) + (1 if int(m.group(2)) else 0)
    m = re.fullmatch(r"\s*(\d+)(?:\s*/\s*(\d+))?(?:\s*\+\s*(\d+))?\s*", text)
    if m:
        return int(m.group(1)) + int(m.group(2) or 0) + int(m.group(3) or 0)
    return 0


def parse_bitrate_kbps(value: str) -> int:
    """Parse bitrate strings like '640 Kb/s' or '28.0 Mb/s' to kbit/s."""
    if not value:
        return 0
    m = re.search(r"([\d.]+)\s*([kKmM])\s*b\s*/?\s*s", value)
    if not m:
        return 0
    n = float(m.group(1))
    return int(n * 1000) if m.group(2).lower() == "m" else int(n)


@dataclass
class Attribute:
    """One CINFO/TINFO/SINFO attribute record."""

    id: int
    code: int
    value: str


@dataclass
class StreamInfo:
    """One stream (video/audio/subtitle) inside a title."""

    tid: int
    sid: int
    attrs: dict[int, Attribute] = field(default_factory=dict)

    def attr(self, attr_id: int) -> Attribute | None:
        return self.attrs.get(attr_id)

    def _value(self, attr_id: int) -> str:
        a = self.attrs.get(attr_id)
        return a.value if a else ""

    @property
    def kind(self) -> str:
        """'video' | 'audio' | 'subtitle' | '' (from TYPE attribute value)."""
        return self._value(1).strip().lower()

    @property
    def codec(self) -> str:
        return self._value(6) or self._value(5)

    @property
    def codec_long(self) -> str:
        return self._value(7)

    @property
    def name(self) -> str:
        return self._value(30)

    @property
    def lang_code(self) -> str:
        return self._value(28).strip().lower()

    @property
    def lang_name(self) -> str:
        return self._value(29)

    @property
    def channels(self) -> int:
        n = parse_channel_count(self._value(11))
        if not n:
            n = parse_channel_count(self.name)
        return n

    @property
    def bitrate_kbps(self) -> int:
        return parse_bitrate_kbps(self._value(13))

    @property
    def resolution(self) -> str:
        return self._value(19)

    @property
    def fps(self) -> float | None:
        raw = self._value(21).split()[0] if self._value(21) else ""
        try:
            return float(raw)
        except ValueError:
            return None

    def all_text(self) -> str:
        return " ".join(a.value for a in self.attrs.values())


@dataclass
class Title:
    """One title (track) on a disc."""

    tid: int
    attrs: dict[int, Attribute] = field(default_factory=dict)
    streams: list[StreamInfo] = field(default_factory=list)

    def attr(self, attr_id: int) -> Attribute | None:
        return self.attrs.get(attr_id)

    def _value(self, attr_id: int) -> str:
        a = self.attrs.get(attr_id)
        return a.value if a else ""

    @property
    def title_name(self) -> str:
        return self._value(2) or self.filename

    @property
    def duration_seconds(self) -> int:
        raw = self._value(9)
        return parse_duration(raw) if raw else 0

    @property
    def chapters(self) -> int:
        try:
            return int(self._value(8))
        except ValueError:
            return 0

    @property
    def filesize_bytes(self) -> int:
        try:
            return int(self._value(11))
        except ValueError:
            return 0

    @property
    def filename(self) -> str:
        v = self._value(27)
        if len(v) >= 2 and v.startswith('"') and v.endswith('"'):
            v = v[1:-1]
        return v

    def streams_of(self, kind: str) -> list[StreamInfo]:
        return [s for s in self.streams if s.kind == kind]


@dataclass
class Disc:
    """A scanned disc and its titles."""

    index: int
    drive_name: str = ""
    disc_label: str = ""
    media_type: str = ""  # 'dvd' | 'bluray' | ''
    attrs: dict[int, Attribute] = field(default_factory=dict)
    titles: list[Title] = field(default_factory=list)

    @property
    def name(self) -> str:
        a = self.attrs.get(2)
        if a and a.value:
            return a.value
        return self.disc_label

    def title(self, tid: int) -> Title | None:
        for t in self.titles:
            if t.tid == tid:
                return t
        return None