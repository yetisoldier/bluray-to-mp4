# Changelog

All notable changes to bluray-to-mp4. Dates are America/Chicago.

## [0.1.2] - 2026-10-01

### Added

- **TMDB episode-title and movie-year autofill** (issue #6, optional).
  A free TMDB API key (themoviedb.org/settings/api) now enables:
  - Series discs: episode titles fetched for the detected season and
    used directly in `Show Name - S01E05 - Title.mp4` filenames.
  - Movie discs: year lookup, so output lands as `Movie Title (2019).mp4`.
  - UI: TMDB key field (persisted) plus "Fetch episode titles from TMDB"
    and "Look up year from TMDB" buttons; lookups run off the UI thread.
  - CLI: `--tmdb-key` flag; `B2M_TMDB_KEY` environment variable also works.
  - No key configured: the feature is dormant and naming stays manual.
    Lookup failures (offline, no match, bad key) degrade to warnings and
    never fail a run.

## [0.1.1] - 2026-10-01

### Fixed

- **"makemkvcon not found on PATH" on healthy machines.** The MakeMKV
  (and Subtitle Edit) Windows installers do not add their CLI tools to
  PATH, so v0.1.0 reported MakeMKV as missing even when installed in the
  default location (`C:\Program Files (x86)\MakeMKV`). All external tools
  are now resolved through a three-tier discovery layer: environment
  override (`B2M_MAKEMKVCON`, `B2M_FFMPEG`, `B2M_FFPROBE`,
  `B2M_SUBTITLE_EDIT`) -> `PATH` -> standard install folders.
- Missing ffmpeg during subtitle extraction now raises a typed error with
  an install hint instead of a raw `FileNotFoundError` traceback.

### Added

- `binaries.py` discovery layer with regression tests for the
  installed-but-off-PATH scenario (reported by a real Windows machine).

## [0.1.0] - 2026-10-01

Initial release. Complete local disc-to-MP4 pipeline.

### Features

- **MakeMKV robot-mode integration**: drive scan, disc scan, title rip
  with live progress; typed errors for beta-key expiry (MSG 5021/5055),
  disc-open failure (5010), and per-title rip failures (5003).
- **Recommendation engine**: best English audio track by channels, codec
  tier, and bitrate; commentary and descriptive-audio tracks excluded.
  Plain English subtitles preferred over SDH, forced-only, and foreign
  tracks. Every pick carries human-readable reasons.
- **Movie vs series detection**: one dominant title means movie (main
  feature = longest); a cluster of similar-length titles (>= 10 minutes
  each) means series, and each becomes an episode.
- **Plex/Jellyfin-standard naming**: `Movie Title (Year).mp4` and
  `Show Name - S01E05 - Episode Title.mp4`, with Windows-illegal
  character handling and reserved-name protection.
- **Encoding**: NVENC hardware mode (`hevc_nvenc`, preset p6 VBR,
  ~10x faster) auto-detected at runtime; x265 software quality mode
  fallback. Bitrate budgets solve for 2 GB/hour on Blu-ray 1080p and
  1.5 GB/hour on DVD (native SD; no upscaling). `yadif` deinterlacing
  for 1080i/interlaced DVD sources. `+faststart` MP4s.
- **Subtitles**: lossless extraction of the recommended PGS/VOBsub
  track, OCR to SRT via Subtitle Edit CLI + Tesseract, then local
  normalization (BOM/CRLF tolerance, inverted-timestamp and zero-length
  cue repair, empty-cue removal). Sidecar `.srt` named to match the
  MP4. OCR failure degrades to a warning, never a lost rip.
- **Desktop UI** (PySide6): Setup check with download links -> drive
  picker -> scan -> recommendations with reasons -> series naming panel
  (show name, season, start episode) or movie panel (title, year) ->
  progress with per-stage status. Inline error banners with actionable
  hints instead of modal dialogs.
- **Monitoring API**: read-only HTTP status server (stdlib only, token
  auth) with `/health`, `/status`, `/history`; feeds from the same
  callbacks as the UI progress screen.
- **CLI**: `drives`, `scan`, `run` with `--dry-run`, `--encoder`,
  series naming flags; `--status` prints a monitor snapshot.
- **Packaging**: PyInstaller one-folder build via GitHub Actions
  (windows-latest runner), offline test suite in CI, exe smoke test,
  zip artifact. Released as `bluray-to-mp4-windows-x64.zip`.

### Known limitations

- No real-disc validation yet: the parser is fixture-tested against
  verified MakeMKV robot-mode output, but the first physical disc is
  the true end-to-end test.
- Subtitle Edit OCR quality is ~95-99%; spot-checking output is
  recommended.
- Episode titles are manual (TMDB autofill is tracked as issue #6).