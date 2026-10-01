"""Persistent user settings (~/.bluray-to-mp4/config.json)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field

__all__ = ["Settings", "load_settings", "save_settings"]

CONFIG_DIR = os.path.expanduser("~/.bluray-to-mp4")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")


@dataclass
class Settings:
    outdir: str = ""                      # last-used output directory
    encoder: str = "auto"                  # 'auto' | 'nvenc' | 'x265'
    monitor_host: str = "127.0.0.1"        # 0.0.0.0 enables LAN monitoring
    monitor_port: int = 8765
    monitor_token: str = ""                # empty = local-only convenience
    monitor_enabled: bool = True
    tmdb_key: str = ""                  # free key: themoviedb.org/settings/api
    last_series_name: str = ""
    extras: dict = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Settings":
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
            known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
            return cls(**{k: v for k, v in data.items() if k in known})
        except (OSError, json.JSONDecodeError, TypeError):
            return cls()

    def save(self) -> None:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2)


def load_settings() -> Settings:
    return Settings.load()


def save_settings(settings: Settings) -> None:
    settings.save()