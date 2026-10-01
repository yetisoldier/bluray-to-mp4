"""Persistent user settings (~/.bluray-to-mp4/config.json).

Private repo, so LAN monitoring ships enabled by default (v0.1.3+): the
read-only monitor binds 0.0.0.0:8765 with a baked token. A checkbox in
the app turns it on/off live and the choice persists. Rotate
MONITOR_TOKEN_DEFAULT if this repo's visibility ever changes.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field

__all__ = ["Settings", "load_settings", "save_settings",
           "MONITOR_TOKEN_DEFAULT", "MONITOR_CFG_VERSION"]

CONFIG_DIR = os.path.expanduser("~/.bluray-to-mp4")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# Baked LAN-monitor defaults (private repo; the API is strictly read-only).
MONITOR_TOKEN_DEFAULT = "b2m-mon-7c4f9a2e-8d31-4b6c-9e2f-1a5d3c8e7f42"
MONITOR_CFG_VERSION = 2


@dataclass
class Settings:
    outdir: str = ""                      # last-used output directory
    encoder: str = "auto"                 # 'auto' | 'nvenc' | 'x265'
    monitor_enabled: bool = True          # LAN status page on by default
    monitor_host: str = "0.0.0.0"         # LAN-visible (v0.1.3+)
    monitor_port: int = 8765
    monitor_token: str = MONITOR_TOKEN_DEFAULT
    monitor_cfg_version: int = MONITOR_CFG_VERSION
    tmdb_key: str = ""                    # free key: themoviedb.org/settings/api
    last_series_name: str = ""
    theme: str = "dark"                   # 'dark' | 'light'
    extras: dict = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Settings":
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return cls()
        known = set(cls.__dataclass_fields__)
        try:
            settings = cls(**{k: v for k, v in data.items() if k in known})
        except TypeError:
            return cls()
        # Pre-v0.1.3 configs predate the LAN defaults and the in-app
        # toggle; their monitor fields were undocumented, so adopt the
        # current defaults (the user's last toggle choice wins from
        # here on). Configs already at the current version are honored
        # exactly as written.
        if data.get("monitor_cfg_version", 0) < MONITOR_CFG_VERSION:
            defaults = cls()
            for name in ("monitor_enabled", "monitor_host", "monitor_port",
                         "monitor_token", "monitor_cfg_version"):
                setattr(settings, name, getattr(defaults, name))
        return settings

    def save(self) -> None:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2)


def load_settings() -> Settings:
    return Settings.load()


def save_settings(settings: Settings) -> None:
    settings.save()