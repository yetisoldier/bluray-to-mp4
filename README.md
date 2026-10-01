# bluray-to-mp4

Rip personal Blu-ray and DVD discs to MP4 with sensible defaults: 1080p HEVC
at ~2 GB per hour (Blu-ray), native-resolution SD at ~1.5 GB per hour (DVD),
AAC 5.1 audio, English subtitles, and Plex/Jellyfin-standard naming for both
movies and TV series. Built for people who don't want to learn what a "PGS
stream" is: the app scans the disc and recommends which tracks to keep, with
plain-English reasons, while keeping every choice overridable.

> Personal backups of discs you own only. Keep the tool and its output
> private; circumvention law varies by jurisdiction.

**Latest release: [v0.1.1](../../releases/tag/v0.1.1)** (Windows x64, exe included)

## What it does

- **Scan** a disc and see every title, audio track, and subtitle track
- **Recommendations**: best English audio (commentary and descriptive tracks
  skipped), plain English subtitles over SDH/forced/foreign tracks, main
  feature vs series episodes detected automatically
- **Series support**: episodes named `Show Name - S01E05 - Episode Title.mp4`
  with season and starting-episode controls
- **Encode**: NVIDIA NVENC hardware mode when available (~10x faster), x265
  software quality mode otherwise; deinterlacing for 1080i/interlaced DVDs;
  no DVD upscaling (native SD looks the same at half the size)
- **Subtitles**: Blu-ray PGS/DVD VOBsub tracks are OCR'd to sidecar `.srt`
  files named to match the video, so every player picks them up
- **TMDB autofill (optional)**: with a free TMDB key, episode titles land in
  the filenames automatically (`Show - S01E05 - Actual Title.mp4`) and movies
  get their year appended (`Movie Title (2019).mp4`)
- **Monitoring**: built-in read-only HTTP API so progress can be checked
  from another machine on the LAN

## Software dependencies

The app is a desktop GUI (plus CLI) that orchestrates external tools. It
ships as a single folder exe and needs the tools below installed once.

### Required

| Tool | What it's for | Install | Notes |
|------|---------------|---------|-------|
| **MakeMKV** | Disc decryption and title extraction (AACS/BD+). The app drives its command-line engine, `makemkvcon`. | [makemkv.com/download](https://www.makemkv.com/download/) - run the installer, defaults are fine | Free while in beta: the beta key expires roughly monthly. When it does, the app tells you; grab the current key from the [MakeMKV forum](https://www.makemkv.com/forum/viewtopic.php?f=5) and paste it into the MakeMKV GUI (Help > Register). A purchased key is permanent. |
| **ffmpeg + ffprobe** | Video encoding, stream analysis, subtitle extraction. | `winget install ffmpeg` (PowerShell), or a build from [ffmpeg.org/download](https://ffmpeg.org/download.html) | One install provides both `ffmpeg` and `ffprobe`. Any recent release works. |

### Optional

| Tool | What it's for | Install | Notes |
|------|---------------|---------|-------|
| **Subtitle Edit** | OCR of image-based subtitles (PGS/VOBsub) to SRT text. | [github.com/SubtitleEdit/subtitleedit/releases](https://github.com/SubtitleEdit/subtitleedit/releases) - run the installer | Without it, MP4s are produced normally; subtitle OCR is skipped with a note. |
| **Tesseract OCR** | The OCR engine Subtitle Edit calls. | [UB Mannheim build](https://github.com/UB-Mannheim/tesseract/wiki) (includes English data) - run the installer, defaults are fine | Install the English language data when prompted. OCR is ~95-99% accurate; the app notes when output looks sparse. |
| **NVIDIA GPU + current driver** | NVENC hardware encoding (fast mode, roughly 10x quicker than x265). | [nvidia.com/drivers](https://www.nvidia.com/drivers) | Auto-detected at runtime. Without it, the app falls back to x265 software encoding, which is slower but slightly higher quality per bit. |
| **TMDB API key** | Autofills episode titles for series discs and the release year for movies (naming only; nothing else talks to the internet). | Free key: [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api) (Developer key), then paste it into the app's TMDB field | Optional: without a key, episode titles and years are entered manually. Lookups are optional and fail soft (offline/bad key = warning, never a failed run). |

### How the app finds these tools

Installers don't always add their tools to the Windows PATH, so the app
searches in this order:

1. Environment variable override (useful for nonstandard installs):
   - `B2M_MAKEMKVCON` - full path to `makemkvcon.exe`
   - `B2M_FFMPEG` / `B2M_FFPROBE` - full paths to the ffmpeg tools
   - `B2M_SUBTITLE_EDIT` - full path to `SubtitleEdit.exe`
2. The Windows `PATH`
3. Standard install locations: `C:\Program Files\MakeMKV`,
   `C:\Program Files (x86)\MakeMKV`, `%LOCALAPPDATA%\Programs\...`,
   WinGet link folders, `C:\ffmpeg\bin`

The in-app **Setup check** screen shows exactly what was found and links any
missing download page, so you never have to guess.

## Install

### Option A: download the exe (recommended)

1. Download `bluray-to-mp4-windows-x64.zip` from the
   [latest release](../../releases)
2. Extract it anywhere (e.g. `C:\Tools\bluray-to-mp4`) and run
   `bluray-to-mp4.exe`
3. Install the required tools from the table above (the Setup screen
   links them)
4. Insert a disc, press **Scan**

### Option B: from source

```
git clone https://github.com/yetisoldier/bluray-to-mp4.git
cd bluray-to-mp4
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[ui]"
python -m bluray_to_mp4
```

Requires Python 3.11+ ([python.org/downloads](https://www.python.org/downloads/windows/),
check "Add to PATH" during install).

## Use

### GUI

Run `bluray-to-mp4.exe`. The flow: Setup check (first run) -> pick drive ->
Scan -> review recommendations -> adjust naming if you like -> **Start**.
Progress shows per-stage status and the current job; warnings (like sparse
OCR) appear inline.

### CLI (same engine)

```
bluray-to-mp4 drives                     # list optical drives + loaded discs
bluray-to-mp4 scan 0                     # analyze disc 0, show recommendations
bluray-to-mp4 run 0 --out "D:\Movies" --dry-run    # preview the full plan
bluray-to-mp4 run 0 --out "D:\Movies"    # rip + encode for real
```

Series discs are detected automatically; override naming when the disc label
is unhelpful:

```
bluray-to-mp4 run 0 --out "D:\TV" --series-name "Breaking Bad" --season 1 --start-episode 5
```

Force an encoder with `--encoder nvenc` or `--encoder x265` (default: auto).

## How decisions are made

- **Movie vs series**: one dominant long title means movie (longest title is
  the main feature). A cluster of similar-length titles of 10+ minutes means
  series; each becomes an episode.
- **Audio**: highest-scoring English track (channels + codec tier + bitrate).
  Commentary and descriptive-audio tracks are scored out entirely.
- **Subtitles**: plain English over SDH ("English (SDH)" includes sound
  effects), forced-only, and foreign tracks.
- **Sizing**: 2 GB/hour is about 4.4 Mbps total, leaving roughly 3.9 Mbps
  video + 448 kbps AAC 5.1 after container overhead. DVDs get 1.5 GB/hour at
  native resolution.
- **Subtitles in MP4**: PGS subtitles are images and MP4 cannot hold them,
  so the recommended track is OCR'd to a matching sidecar `.srt`.

## Remote monitoring

The app runs a read-only status server. **Since this is a private
repository, LAN monitoring ships enabled by default** (`0.0.0.0:8765` with
a baked token) — no configuration needed.

- **Turn it on/off**: the "LAN monitoring" checkbox on the main screen
  starts/stops the server live and remembers your choice. The status
  line shows the LAN URL when it's on.
- **Check progress** from another machine:

```
curl -H "Authorization: Bearer <token>" http://<windows-pc>:8765/status
curl -H "Authorization: Bearer <token>" http://<windows-pc>:8765/history
curl http://<windows-pc>:8765/health        # token-free liveness ping
```

The baked token is `b2m-mon-7c4f9a2e-8d31-4b6c-9e2f-1a5d3c8e7f42`
(`bluray_to_mp4/config.py`); change it in
`%USERPROFILE%\.bluray-to-mp4\config.json` if you ever want a fresh
one. Windows Firewall will prompt on first launch — allow it on private
networks so the status page is reachable from the LAN.

The server is strictly read-only: it reports progress and can never
trigger rips, encodes, or setting changes.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| "makemkvcon not found" but MakeMKV is installed | The app searches standard install folders, so this means a nonstandard location. Set `B2M_MAKEMKVCON` to the full path of `makemkvcon.exe`. |
| "Evaluation period has expired" | MakeMKV beta key expired. Get the current key from the [forum](https://www.makemkv.com/forum/viewtopic.php?f=5) and register it in MakeMKV. |
| "Failed to open disc" | Check the drive letter, clean the disc, confirm it's a video disc. |
| Encode is slow | No NVENC detected (the Setup screen says). Update the NVIDIA driver, or accept x265 mode. |
| No subtitles next to the MP4 | Subtitle Edit (optional) isn't installed, or OCR found no usable cues; the run log says which. |
| Monitor unreachable from LAN | Check the "LAN monitoring" checkbox is on (main screen). Windows Firewall prompts on first launch — allow it on private networks. |

## Development

```
pip install -e ".[dev]"
pytest
```

The full pipeline is testable offline: `run_pipeline(..., dry_run=True)`
plans everything against fixture MakeMKV output without touching discs,
makemkvcon, or ffmpeg. 53 tests cover the parser, recommendations, series
detection, encode args, SRT handling, binary discovery, UI flows (offscreen
Qt), and the monitoring API.

Docs: [BUILD.md](BUILD.md) (exe packaging + CI), [DEVELOPMENT.md](DEVELOPMENT.md)
(design decisions + dev log), [CHANGELOG.md](CHANGELOG.md).

## Roadmap

All roadmap issues are closed. Done: pipeline + CLI (#1), subtitle OCR (#2),
desktop UI (#3), packaging + dependency checker (#4), NVENC detection (#5),
TMDB episode-title and movie-year autofill (#6). Naming works fully
offline without a TMDB key; the key only adds autofill.

Next milestone: real-disc validation (see DEVELOPMENT.md's pending
validation checklist).

## License

Private repository; all rights reserved.