# Changelog

All notable changes to bluray-to-mp4. Dates are America/Chicago.

## [0.2.6] - 2026-10-02

### Fixed

- **Rip progress was invisible.** MakeMKV robot mode emits no PRGV
  progress lines unless `--progress=-` is passed; the rip command never
  passed it, so the entire rip phase sat at 0% with no ETA while the disc
  decrypted and copied (looked frozen - found in first real-disc
  validation). Rips now stream live percent + a rate-based ETA, and
  MakeMKV status messages (BD+ open etc.) surface in the Encode page and
  monitor while PRGV sits at 0.
- The Encode page opens with an honest "Ripping disc (decryption can take
  a few minutes)" instead of "Waiting to start".

## [0.2.5] - 2026-10-02


### Fixed

- **The scan no longer feels frozen.** MakeMKV's disc read now streams live
  progress into the Source page (MakeMKV status messages + "Reading title
  N" lines) instead of a static label while the table of contents is read.
- **Honest Setup screen.** The button reads "Continue" when everything is
  ready, with a tooltip naming any optional gaps; "Continue anyway" now
  appears only when a REQUIRED tool is actually missing (the old label
  showed even when all six checks passed).

## [0.2.4] - 2026-10-02


### Fixed

- **NVENC now actually works on driver 591.x.** v0.2.2/0.2.3 bundled
  ffmpeg n9.0, which is compiled against NVENC SDK 13.1 and demands
  driver 610+ ("Driver does not support the required nvenc API version.
  Required: 13.1 Found: 13.0"). The bundle is now ffmpeg n8.1 (NVENC
  SDK 13.0, needs only driver 590+), matching current shipped NVIDIA
  drivers. No driver update required.

## [0.2.3] - 2026-10-02


### Added

- **YetiWurks LLC publisher metadata**: the exe's file properties (and any
  Windows dialog) now show YetiWurks LLC as publisher, with product version
  0.2.3 and proper copyright.
- **Code-signed builds**: every CI build is signed with the YetiWurks code
  signing certificate (embedded signature, RFC3161 timestamp). Install the
  bundled `YetiWurks-CodeSigning.cer` once on each PC (see README) and
  Windows stops warning that the app is from an unknown publisher.
- About dialog credits YetiWurks LLC.

## [0.2.2] - 2026-10-02


### Fixed

- **NVENC now works in the packaged app.** The bundled ffmpeg is the full
  NVENC-capable build, and packaged builds prefer their own bundled tools
  over anything on PATH (a system ffmpeg without NVENC was shadowing ours).
  CI verifies hevc_nvenc is present in the bundled build before packaging.
- **Setup screen tells you *why* NVENC failed**: driver/hardware problem vs
  software-only ffmpeg build - no more guessing.
- **Subtitle Edit ships in the zip.** The portable SubtitleEdit is bundled
  into the app (packaging/tools/SubtitleEdit), so subtitle OCR works with
  zero extra installs. Search order: env override > bundled > PATH > install dirs.

## [0.2.1] - 2026-10-01


### Fixed (Karen stage-3 validation, all landed & tested)

- **D1** CLI real-run progress crash: encode progress emits 4-tuples, CLI now
  tolerates both shapes (with a regression test)
- **D2** Season spin starts at 1 (fresh series discs no longer preview/rip
  as S00E01)
- **D3** Naming edits live-refresh previews (season, start-episode,
  series name, movie title, year wired to _update_previews)
- **D4** Setup screen finds ffmpeg wherever the encoder does
- **D5** Partial failures read honestly ("P of N done, 1 failed")
- G1: Descriptive narration subtitles excluded (same family as commentary)
- G2: Commentary-only discs warn instead of silently recommending commentary
- G3: Missing subtitle stream after rip warns (was silent drop)
- G4: B2M_TMDB_KEY env var honored (CLI-help promise kept)
- G8: Expired-key detection uses MSG codes, not substring (no false positives)
- G9: History entries carry real durations + per-job warnings
- G10: PermissionError from ffmpeg subprocess caught as typed error
- G11: Dead import removed
- G12: Vacuous test asserts replaced with real ones
- G13: Per-episode titles editable in the Review table (double-click); the
  CHANGELOG's v0.2.0 claim is now actually true

## [0.2.0] - 2026-10-01
## [0.2.0] - 2026-10-01

### Changed

- **Complete UI overhaul.** The desktop app now uses a real design system
  (theme.py, dark/light palettes) with sidebar navigation, cards, browse-
  for-folder, live output-filename previews, per-episode editable titles,
  and a History page. All colors come from the palette; nothing hardcodes
  them (light mode works by switching the palette).
- **Encoding pipeline improvements:** per-stage ETA shown in the UI
  (elapsed-based ffmpeg estimate), cancel button stops ffmpeg gracefully,
  overall queue progress + per-job progress bars.
- **Bundled ffmpeg/ffprobe** in CI builds (downloaded from ffmpeg.org at
  build time, placed in packaging/tools, found automatically by the
  discovery layer). One less manual install step.
- **Status pill** in the header shows monitor state (LAN URL when on).

### Fixed

- The frozen-exe entry now passes the binary name through correctly (the
  previous version incorrectly included it inside the args list).

## [0.1.3] - 2026-10-01

### Added

- **LAN monitoring on by default** with an in-app on/off toggle. Since this
  is a private repository, the read-only status server ships enabled so
  another machine can check rip progress with zero manual configuration;
  the checkbox starts/stops the server live and persists the choice.

## [0.1.2] - 2026-10-01

### Added

- **TMDB episode-title and movie-year autofill** (issue #6, optional).

## [0.1.1] - 2026-10-01

### Fixed

- **"makemkvcon not found on PATH"** on healthy machines: installers don't add
  CLI tools to PATH, so tools are now discovered via env override -> PATH ->
  standard install folders (Program Files, LOCALAPPDATA, WinGet links).

## [0.1.0] - 2026-10-01

Initial release. Complete local disc-to-MP4 pipeline.
