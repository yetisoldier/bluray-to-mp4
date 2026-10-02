# Build the Windows app

Two ways to produce the exe. The GitHub Actions path is the primary one
(it is how every release so far was built) because the dev box is Linux
and PyInstaller does not cross-compile.

## Option A: GitHub Actions (primary, no local tooling needed)

1. Push your changes to `main` on
   https://github.com/yetisoldier/bluray-to-mp4
2. Actions tab -> **build-windows** -> Run workflow
   (or `gh workflow run build-windows.yml`)
3. Wait ~2-3 minutes; the run does, in order:
   - installs the package + PySide6 + PyInstaller on a windows-latest runner
   - runs the full test suite offscreen (fails the build on any red test)
   - builds the exe via `bluray_to_mp4.spec`
   - smoke-tests the exe (`bluray-to-mp4.exe --status` must exit 0)
   - uploads `bluray-to-mp4-windows-x64.zip` as an artifact (30 days)
**Bundled ffmpeg** (v0.2.0+): CI downloads the gyan.dev static build into `packaging/tools` before PyInstaller runs, so the zip includes ffmpeg/ffprobe. 4. Download from the run page, or attach to a release:
   `gh release create vX.Y.Z <zip> --title ... --notes ...`

## Option B: local Windows build

PowerShell on the Windows PC:

```powershell
git clone https://github.com/yetisoldier/bluray-to-mp4.git
cd bluray-to-mp4

python -m venv .venv
.venv\Scripts\activate
pip install -e ".[ui,dev]"

pytest                # optional but recommended
pyinstaller bluray_to_mp4.spec --noconfirm
```

Output: `dist\bluray-to-mp4\bluray-to-mp4.exe`, runs without Python
installed. Copy the whole folder anywhere (or pin the exe to the
taskbar) and double-click.

## PyInstaller notes

- The spec entry is `run_app.py`, which imports
  `bluray_to_mp4.__main__` as a package module. Do not point the spec at
  `__main__.py` directly: PyInstaller runs the entry as a plain script,
  and the package-relative imports then break inside the frozen exe
  (this failed the v0.1.0 CI smoke test until fixed).
- `console=True` is deliberate: the console shows the monitor URL on
  startup and first-run diagnostics. A windowed build would hide them.
- External tools are **not** bundled; see the README dependency table.
  The app's Setup check verifies them at first launch and the discovery
  layer finds them off-PATH (v0.1.1+).

## Releases so far

| Tag | Date | What |
|-----|------|------|
| v0.1.0 | 2026-10-01 | First packaged exe (CI build) |
| v0.1.1 | 2026-10-01 | Discovery layer: finds MakeMKV/ffmpeg/Subtitle Edit outside PATH |
| v0.1.2 | 2026-10-01 | TMDB episode-title + movie-year autofill (issue #6 closed) |
| v0.1.3 | 2026-10-01 | LAN monitoring on by default + in-app on/off toggle |

## Remote monitoring (for Jarvis)

Ships enabled (v0.1.3+): the read-only status server binds
`0.0.0.0:8765` with a baked token (`bluray_to_mp4/config.py`). Eric can
toggle it with the "LAN monitoring" checkbox in the app; the choice
persists in `%USERPROFILE%\.bluray-to-mp4\config.json`. Windows
Firewall prompts on first launch - allow on private networks.

Query from the agent box:

```
curl -H "Authorization: Bearer <baked token from config.py>" http://<windows-pc>:8765/status
curl http://<windows-pc>:8765/health   # token-free liveness ping
```
