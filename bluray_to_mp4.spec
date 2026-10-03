"""PyInstaller spec: one-folder Windows build with the UI + CLI.

Build on a Windows machine with:
    pip install pyinstaller PySide6
    pyinstaller bluray_to_mp4.spec --noconfirm

The dist folder runs without Python installed. CI bundles ffmpeg (full
NVENC-capable build) and Subtitle Edit portable into the app zip, so only
MakeMKV remains a separate install. The in-app dependency checker walks
users through anything missing.
"""

import os
import glob
import subprocess
import sys

block_cipher = None

_icon_path = os.path.join("packaging", "app_icon.ico")
if not os.path.exists(_icon_path):
    subprocess.check_call([sys.executable, os.path.join("packaging", "build_icon.py"), _icon_path])

# Optional bundled ffmpeg (downloaded in CI, or placed at
# packaging/tools/ locally before building). Empty when absent.
_tools_dir = os.path.join("packaging", "tools")
_datas = [(os.path.join("bluray_to_mp4", "ui", "app_icon.svg"), os.path.join("bluray_to_mp4", "ui"))]
if os.path.isdir(_tools_dir):
    _datas.append((_tools_dir, "tools"))

hiddenimports = [
    "bluray_to_mp4",
    "bluray_to_mp4.cli",
    "bluray_to_mp4.makemkv",
    "bluray_to_mp4.models",
    "bluray_to_mp4.recommend",
    "bluray_to_mp4.series",
    "bluray_to_mp4.naming",
    "bluray_to_mp4.encode",
    "bluray_to_mp4.ffprobe",
    "bluray_to_mp4.pipeline",
    "bluray_to_mp4.streamsource",
    "bluray_to_mp4.srt",
    "bluray_to_mp4.subtitles",
    "bluray_to_mp4.upscale",
    "bluray_to_mp4.monitor",
    "bluray_to_mp4.monitor_client",
    "bluray_to_mp4.deps",
    "bluray_to_mp4.config",
    "bluray_to_mp4.ui",
    "bluray_to_mp4.ui.app",
    "bluray_to_mp4.ui.main_window",
    "bluray_to_mp4.ui.workers",
]

a = Analysis(
    ["run_app.py"],
    pathex=["."],
    binaries=[],
    datas=_datas,
    hiddenimports=hiddenimports + ["run_app"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "PIL"],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="bluray-to-mp4",
    console=True,  # keep console for first-run dependency output + monitor URL
    icon=_icon_path,
    version="packaging/version_info.txt",  # YetiWurks LLC publisher metadata
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="bluray-to-mp4",
)
# Keep packaging/tools out of version control; CI downloads ffmpeg there.
# .gitignore will ignore it.