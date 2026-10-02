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

def test_bundled_tools_beat_path_when_frozen(tmp_path, monkeypatch):
    """Packaged app: the bundled ffmpeg/SubtitleEdit win over any random
    system copy on PATH (regression: NVENC probe hit a PATH ffmpeg that
    lacked hevc_nvenc)."""
    import shutil as _shutil
    import sys
    from bluray_to_mp4 import binaries

    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "ffmpeg.exe").write_text("fake bundled")
    se_dir = tools / "SubtitleEdit"
    se_dir.mkdir()
    (se_dir / "SubtitleEdit.exe").write_text("fake bundled se")

    monkeypatch.setattr(
        sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    # A "system PATH" ffmpeg also exists - bundled one must still win.
    sys_dir = tmp_path / "sys"
    sys_dir.mkdir()
    (sys_dir / "ffmpeg.exe").write_text("fake path copy")
    monkeypatch.setattr(_shutil, "which", lambda name: str(sys_dir / f"{name}.exe"))

    found = binaries.find_program("ffmpeg", exe_names=["ffmpeg.exe", "ffmpeg"])
    assert found is not None
    assert "tools" in found and "sys" not in found

    se = binaries.find_program(
        "SubtitleEdit", search_dirs=binaries._subtitle_edit_dirs(),
        exe_names=["SubtitleEdit.exe", "SubtitleEdit"])
    assert se is not None and "SubtitleEdit" in se


def test_nvenc_check_distinguishes_build_vs_driver(monkeypatch, tmp_path):
    """The NVENC miss message names the real cause (build lacks encoder vs
    hardware probe failed), so Setup tells you what to fix."""
    from bluray_to_mp4 import deps
    import subprocess as _sp

    fake_ffmpeg = tmp_path / "ffmpeg.exe"
    fake_ffmpeg.write_text("x")

    class _Proc:
        def __init__(self, rc, out=""):
            self.returncode = rc
            self.stdout = out
            self.stderr = ""

    # ffmpeg reports hevc_nvenc among encoders but the probe fails:
    # message should mention drivers/GPU, not "build lacks".
    calls = {"probe": 0}

    def fake_run(cmd, **kw):
        if "-encoders" in cmd:
            calls["probe"] += 1
            return _Proc(0, " ... hevc_nvenc ... ")
        return _Proc(1)

    monkeypatch.setattr(_sp, "run", fake_run)
    monkeypatch.setattr(
        deps, "find_program",
        lambda *a, **k: str(fake_ffmpeg))
    check = deps.check_nvenc()
    assert not check.ok
    assert "driver" in check.detail.lower() or "probe" in check.detail.lower()

    # ffmpeg without hevc_nvenc: message should say the build lacks it.
    def fake_run2(cmd, **kw):
        if "-encoders" in cmd:
            return _Proc(0, " ... libx265 ... ")
        return _Proc(1)

    monkeypatch.setattr(_sp, "run", fake_run2)
    check2 = deps.check_nvenc()
    assert not check2.ok
    assert "build" in check2.detail.lower()
