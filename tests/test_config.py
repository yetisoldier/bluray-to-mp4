"""Tests for persisted settings: LAN defaults, migration, toggle persistence."""

import json

from bluray_to_mp4 import config as cfg
from bluray_to_mp4.config import MONITOR_TOKEN_DEFAULT, Settings


def test_defaults_are_lan_enabled():
    s = Settings()
    assert s.monitor_enabled is True
    assert s.monitor_host == "0.0.0.0"
    assert s.monitor_port == 8765
    assert s.monitor_token == MONITOR_TOKEN_DEFAULT


def test_pre_v013_config_migrated_to_lan_defaults(tmp_path, monkeypatch):
    """A v0.1.2-era localhost config adopts the baked LAN defaults."""
    monkeypatch.setattr(cfg, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    old = {
        "outdir": "D:\\Movies",
        "encoder": "auto",
        "monitor_host": "127.0.0.1",
        "monitor_port": 8765,
        "monitor_token": "",
        "monitor_enabled": True,
        "tmdb_key": "k",
        "last_series_name": "Show",
    }
    (tmp_path / "config.json").write_text(json.dumps(old), encoding="utf-8")

    s = Settings.load()
    assert s.monitor_host == "0.0.0.0"
    assert s.monitor_token == MONITOR_TOKEN_DEFAULT
    assert s.monitor_cfg_version == cfg.MONITOR_CFG_VERSION
    # Non-monitor settings survive the migration.
    assert s.outdir == "D:\\Movies"
    assert s.tmdb_key == "k"


def test_current_version_config_honored_as_written(tmp_path, monkeypatch):
    """A config at the current version is never rewritten by migration."""
    monkeypatch.setattr(cfg, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    written = {
        "monitor_enabled": False,
        "monitor_host": "127.0.0.1",
        "monitor_port": 9999,
        "monitor_token": "user-token",
        "monitor_cfg_version": cfg.MONITOR_CFG_VERSION,
    }
    (tmp_path / "config.json").write_text(json.dumps(written), encoding="utf-8")

    s = Settings.load()
    assert s.monitor_enabled is False
    assert s.monitor_host == "127.0.0.1"
    assert s.monitor_port == 9999
    assert s.monitor_token == "user-token"


def test_toggle_choice_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    s = Settings()
    s.monitor_enabled = False          # user toggles it off in the UI
    s.save()
    reloaded = Settings.load()
    assert reloaded.monitor_enabled is False  # choice sticks (v2 file)


def test_lan_ip_helper_returns_something():
    from bluray_to_mp4.monitor import lan_ip
    ip = lan_ip()
    assert ip and len(ip.split(".")) == 4  # any dotted quad is fine