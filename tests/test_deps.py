"""Tests for the Setup screen dependency checks (deps.check_subtitle_edit).

v0.2.37 (L6): a green Setup row must mean the headless `seconv`
converter (the binary the OCR wrapper will actually drive) - the
Subtitle Edit GUI binary is refused at run time by
subtitles._refuse_gui_binary, so it can never light the row green.
"""

import os

from bluray_to_mp4 import deps


def _patch_find_program(monkeypatch, result, calls=None):
    """Replace deps.find_program, recording its exe_names argument."""

    def fake_find_program(name, *, env_var=None, search_dirs=None,
                          exe_names=None):
        if calls is not None:
            calls.append(exe_names)
        return result

    monkeypatch.setattr(deps, "find_program", fake_find_program)


def test_check_subtitle_edit_found_via_find_program(monkeypatch):
    """find_program resolves seconv -> the row reports found at that path."""
    _patch_find_program(monkeypatch, "/opt/se/tools/seconv.exe")
    monkeypatch.setattr(os.path, "exists", lambda path: False)

    check = deps.check_subtitle_edit()
    assert check.ok is True
    assert check.detail == "found at /opt/se/tools/seconv.exe"
    assert check.required is False


def test_check_subtitle_edit_probes_seconv_not_gui_in_install_dirs(
        monkeypatch):
    """Fallback probes target seconv.exe inside the well-known SE install
    dirs (standalone installs ship it next to SubtitleEdit.exe), and the
    PATH lookup's exe list no longer contains the GUI binary names at
    all - green must mean what the runtime will do."""
    calls = []
    _patch_find_program(monkeypatch, None, calls)

    probed = []

    def fake_exists(path):
        probed.append(path)
        return str(path).endswith("Subtitle Edit\\seconv.exe")

    monkeypatch.setattr(os.path, "exists", fake_exists)

    check = deps.check_subtitle_edit()
    assert check.ok is True
    assert "seconv.exe" in check.detail
    # The check's own lookup list is headless-only now.
    assert calls[0] == ["seconv.exe", "seconv"]
    assert not any("SubtitleEdit" in str(name) for name in calls[0])
    # The first probed install-dir candidate is a seconv path (not the
    # GUI exe); the loop returns at the first hit.
    assert probed == [r"C:\Program Files\Subtitle Edit\seconv.exe"]


def test_check_subtitle_edit_not_found_reports_ocr_disabled(monkeypatch):
    """Nothing found: the row stays red and keeps the honest
    fail-open message (OCR disabled, MP4s still work)."""
    _patch_find_program(monkeypatch, None)
    monkeypatch.setattr(os.path, "exists", lambda path: False)

    check = deps.check_subtitle_edit()
    assert check.ok is False
    assert "subtitle OCR disabled (MP4s still work)" in check.detail


def test_check_subtitle_edit_gui_only_install_is_not_green(monkeypatch):
    """THE L6 pin: a GUI-only Subtitle Edit install (SubtitleEdit.exe on
    disk, no seconv anywhere) must NOT light the row green - the OCR
    wrapper refuses the GUI binary at run time."""
    _patch_find_program(monkeypatch, None)

    def fake_exists(path):
        return "SubtitleEdit.exe" in str(path)

    monkeypatch.setattr(os.path, "exists", fake_exists)

    check = deps.check_subtitle_edit()
    assert check.ok is False
    assert "subtitle OCR disabled (MP4s still work)" in check.detail