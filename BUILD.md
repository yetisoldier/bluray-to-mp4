# Build the Windows app

Run this on the **Windows PC** (PowerShell), not on the dev box:

```powershell
git clone https://github.com/yetisoldier/bluray-to-mp4.git
cd bluray-to-mp4

python -m venv .venv
.venv\Scripts\activate
pip install -e . PySide6 pyinstaller

pyinstaller bluray_to_mp4.spec --noconfirm
```

Output: `dist\bluray-to-mp4\bluray-to-mp4.exe` — runs without Python.
Copy that folder anywhere (or pin it to the taskbar) and double-click.

## External tools the app drives

| Tool | Install | Why |
|------|---------|-----|
| **MakeMKV** | https://www.makemkv.com/download/ | Decrypts the disc, extracts titles (the app drives its CLI robot mode) |
| **ffmpeg + ffprobe** | https://ffmpeg.org/download.html or `winget install ffmpeg` | Encode + stream analysis |
| **Subtitle Edit** | https://github.com/SubtitleEdit/subtitleedit/releases | PGS→SRT subtitle OCR (optional; MP4s work without it) |
| **Tesseract** | https://github.com/UB-Mannheim/tesseract/wiki (UB Mannheim build, includes English data) | OCR engine used by Subtitle Edit |

The app's **Setup check** screen (first launch) verifies each one and links
the download page. MakeMKV is free while in beta — the beta key is posted
on their forum (https://www.makemkv.com/forum/viewtopic.php?f=5); the app
detects an expired key and tells you where to get the new one.

## Remote monitoring (for Jarvis)

The app runs a read-only status server so progress is checkable from
another machine on the LAN. Configure once in
`%USERPROFILE%\.bluray-to-mp4\config.json`:

```json
{
  "monitor_enabled": true,
  "monitor_host": "0.0.0.0",
  "monitor_port": 8765,
  "monitor_token": "<random string>",
  "encoder": "auto",
  "outdir": "D:\\Movies"
}
```

Then from the agent box:

```
curl -H "Authorization: Bearer <token>" http://<windows-pc>:8765/status
curl -H "Authorization: Bearer <token>" http://<windows-pc>:8765/history
```

`/health` is token-free for a quick liveness ping. The server is strictly
read-only: it can never trigger rips, encodes, or settings changes.