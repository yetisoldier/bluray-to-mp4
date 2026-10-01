"""PyInstaller spec: one-folder Windows build with the UI + CLI.

Build on a Windows machine with:
    pip install pyinstaller PySide6
    pyinstaller bluray_to_mp4.spec --noconfirm

The dist folder runs without Python installed. MakeMKV, ffmpeg, and
Subtitle Edit remain external installs (see README); the in-app
dependency checker walks users through them.
"""

block_cipher = None

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
    "bluray_to_mp4.srt",
    "bluray_to_mp4.subtitles",
    "bluray_to_mp4.monitor",
    "bluray_to_mp4.deps",
    "bluray_to_mp4.config",
    "bluray_to_mp4.ui",
    "bluray_to_mp4.ui.app",
    "bluray_to_mp4.ui.main_window",
    "bluray_to_mp4.ui.workers",
]

a = Analysis(
    ["bluray_to_mp4/__main__.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
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
    icon=None,
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