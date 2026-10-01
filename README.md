# bluray-to-mp4

Rip personal Blu-ray and DVD discs to MP4 with sensible defaults: 1080p HEVC
at ~2 GB/hour (Blu-ray), native-resolution SD at ~1.5 GB/hour (DVD), AAC 5.1
audio, English subtitles, and Plex/Jellyfin-standard naming for both movies
and TV series. Built for people who don't want to learn what a "PGS stream"
is — the app scans the disc and recommends which tracks to keep, with plain
reasons, while keeping every choice overridable.

> Personal backups of discs you own only. Keep the tool and its output
> private; circumvention law varies by jurisdiction.

## Status

- ✅ MakeMKV robot-mode parser (drive scan, disc scan, rip with progress,
  typed errors: beta-key expiry, disc-open failure, rip failure)
- ✅ Recommendation engine (best English audio, skips commentary; plain
  English subtitles over SDH/forced/foreign tracks)
- ✅ Series detection from title durations + `SxxExx - Title.mp4` naming
- ✅ Encode command builder (NVENC fast mode / x265 quality mode,
  2 GB/h and 1.5 GB/h budget math, yadif deinterlace, no DVD upscaling)
- ✅ End-to-end pipeline + CLI (`drives`, `scan`, `run`, `--dry-run`)
- 🚧 PGS subtitle OCR to sidecar SRT — [issue #2](../../issues/2)
- 🚧 PySide6 desktop UI — [issue #3](../../issues/3)
- 🚧 Windows packaging + dependency checker — [issue #4](../../issues/4)

Full roadmap: [issues](../../issues)

## Install (Windows)

1. **Python 3.11+** — python.org, check "Add to PATH" during install.
2. **MakeMKV** — makemkv.com. Works free with the current beta key from
   their forum (the app tells you when it expires and where to get a new one).
3. **ffmpeg** — download from ffmpeg.org, or `winget install ffmpeg`.
4. Clone and install this repo:

   ```
   git clone https://github.com/yetisoldier/bluray-to-mp4.git
   cd bluray-to-mp4
   pip install -e .
   ```

## Use

```
bluray-to-mp4 drives                      # list optical drives + loaded discs
bluray-to-mp4 scan 0                      # analyze disc 0, show recommendations
bluray-to-mp4 run 0 --out "D:\Movies" --dry-run   # preview the full plan
bluray-to-mp4 run 0 --out "D:\Movies"    # rip + encode for real
```

Series discs are detected automatically. Override the naming when the disc
label is unhelpful:

```
bluray-to-mp4 run 0 --out "D:\TV" --series-name "Breaking Bad" --season 1 --start-episode 5
```

Encodes default to NVENC (`hevc_nvenc`) when an NVIDIA GPU is detected and
fall back to x265 slow otherwise. Force either with `--encoder nvenc|x265`.

## How decisions are made

- **Movie vs series**: one dominant title → movie (longest title is the
  main feature). A cluster of similar-length titles ≥10 min → series disc;
  every long title becomes an episode.
- **Audio**: highest-scoring English track (channels + codec tier +
  bitrate). Commentary and descriptive-audio tracks are scored out.
- **Subtitles**: plain English over SDH ("English (SDH)" includes sound
  effects), forced tracks, and foreign languages.
- **Sizing**: 2 GB/hour ≈ 4.44 Mbps total (≈3.9 Mbps video + 448 kbps AAC
  5.1) for Blu-ray; 1.5 GB/hour for DVD at native SD resolution. Upscaling
  SD sources is deliberately not done — it bloats the file with no detail.
- **Subtitles in MP4**: Blu-ray PGS subtitles are images and MP4 can't hold
  them; the recommended track is OCR'd to a sidecar `.srt` (issue #2), which
  every player loads automatically when named to match the video.

## Development

```
pip install -e ".[dev]"
pytest
```

The full pipeline is testable offline: `run_pipeline(..., dry_run=True)`
plans everything against fixture MakeMKV output without touching real
discs, makemkvcon, or ffmpeg.

## License

Private repository; all rights reserved.