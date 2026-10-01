"""Tests for Windows binary discovery (PATH missing, install dirs present)."""

import os
import sys

import pytest

from bluray_to_mp4.binaries import find_program, resolve_makemkvcon


def test_find_program_env_override(tmp_path, monkeypatch):
    fake = tmp_path / "makemkvcon.exe"
    fake.write_bytes(b"stub")
    monkeypatch.setenv("B2M_MAKEMKVCON", str(fake))
    assert resolve_makemkvcon() == str(fake)


def test_find_program_falls_back_to_name(tmp_path, monkeypatch):
    # Nothing found: resolver returns the bare name so callers raise their
    # typed, hint-rich errors (the UX you saw in v0.1.0 stays intact).
    monkeypatch.setenv("B2M_MAKEMKVCON", "")
    monkeypatch.setattr("bluray_to_mp4.binaries.shutil.which", lambda n: None)
    assert resolve_makemkvcon() == "makemkvcon"


def test_find_program_install_dir(tmp_path, monkeypatch):
    """The exact Eric scenario: installed, NOT on PATH."""
    install = tmp_path / "Program Files (x86)" / "MakeMKV"
    install.mkdir(parents=True)
    (install / "makemkvcon.exe").write_bytes(b"stub")

    monkeypatch.setenv("B2M_MAKEMKVCON", "")
    monkeypatch.setattr("bluray_to_mp4.binaries.shutil.which", lambda n: None)
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "Program Files (x86)"))
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))

    from bluray_to_mp4 import binaries
    found = binaries.find_program(
        "makemkvcon",
        search_dirs=binaries._makemkv_dirs(),
        exe_names=["makemkvcon.exe", "makemkvcon64.exe", "makemkvcon"],
    )
    assert found is not None
    assert found.endswith("MakeMKV" + os.sep + "makemkvcon.exe")
    assert resolve_makemkvcon() == found


def test_deps_check_finds_installed_makemkv(tmp_path, monkeypatch):
    """Setup screen must report Ready when MakeMKV is installed off-PATH."""
    install = tmp_path / "Program Files (x86)" / "MakeMKV"
    install.mkdir(parents=True)
    fake_con = install / "makemkvcon.exe"
    fake_con.write_bytes(b"stub")

    class FakeProc:
        returncode = 0
        stdout = "MSG:1005 ... MakeMKV v1.17.9 win(x64-release) started\r\n"
        stderr = ""

    def fake_run(cmd, **kw):
        if str(fake_con) in str(cmd[0]):
            return FakeProc()
        raise OSError("not found")

    monkeypatch.setenv("B2M_MAKEMKVCON", "")
    monkeypatch.setattr("bluray_to_mp4.binaries.shutil.which", lambda n: None)
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "Program Files (x86)"))
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr("bluray_to_mp4.deps.subprocess.run", fake_run)

    from bluray_to_mp4.deps import check_makemkv
    check = check_makemkv()
    assert check.ok, check.detail
    assert "1.17.9" in check.detail
    # And the version probe used the discovered full path.
    assert str(fake_con) == os.path.join(str(install), "makemkvcon.exe")


def test_makemkv_runner_uses_discovered_path(tmp_path, monkeypatch):
    """find_drives must launch the resolved binary, not a bare name."""
    install = tmp_path / "Program Files (x86)" / "MakeMKV"
    install.mkdir(parents=True)
    fake_con = install / "makemkvcon.exe"
    fake_con.write_bytes(b"stub")

    captured = {}

    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        raise FileNotFoundError(cmd[0])  # abort after capture; path is what matters

    monkeypatch.setenv("B2M_MAKEMKVCON", str(fake_con))
    monkeypatch.setattr("bluray_to_mp4.makemkv.subprocess.Popen", fake_popen)

    from bluray_to_mp4.makemkv import find_drives
    with pytest.raises(Exception):
        find_drives()  # raises MakeMkvError from the FileNotFoundError
    assert captured["cmd"][0] == str(fake_con)