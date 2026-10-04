# Changelog

All notable changes to bluray-to-mp4. Dates are America/Chicago.

## [0.2.25] - 2026-10-03

### Fixed

- **DVD AI upscale no longer corrupts frames through realesrgan-x4plus.**
  The 720p profile ran the x4plus model with `-s 2`, which corrupts this
  DVD frame path. The stage now runs x4plus at its native 4x scale and
  downsamples the mezzanine segment to the target height with lanczos
  (`scale=-2:H`, SAR 1). The anime profile keeps realesr-animevideov3
  at its intended 2x with an unscaled segment pass.

### Changed

- **Real-ESRGAN auto tiling.** The tile argument is now `-t 0`
  (model/driver auto tiling), replacing v0.2.24's extent-derived manual
  tile size. Chunk directories are cleared before reuse, and each
  chunk logs input/output frame counts and first-PNG extents to
  `upscale_debug.log` for field diagnosis.

### Added

- **Source geometry plumbing for the upscale.** ffprobe now parses
  stream height, sample aspect ratio, and display aspect ratio and
  exposes `first_video_stream`; the pipeline passes that geometry into
  the upscale plan so frame extraction renders square-pixel 360p
  frames from the disc's actual geometry (fixed size when the probe
  reports dimensions, DAR expression fallback otherwise).

### Validated

- Full local test suite: 253 passed.

## [0.2.24] - 2026-10-03

### Fixed

- **DVD AI upscale now preserves the disc's display aspect ratio.** Frame
  extraction and the mezzanine segment encoder previously used
  `scale=-2:H`, which scales the storage pixel grid but keeps non-square
  DVD pixels, so full-frame 4:3 and anamorphic 16:9 sources rendered with
  the wrong shape. Both stages now compute the square-pixel width from
  the input DAR (`scale=w='trunc(H*dar/2)*2':h=H`, SAR forced to 1), so
  the written frames match the disc's intended geometry.

- **Dynamic tile sizing for the AI upscale.** v0.2.23 forced `-t 2048` to
  stop the GPU driver's auto tile split from leaving seam lines. The
  tile argument is now derived from the first extracted frame's actual
  extent plus a 64px safety margin, rounded up to a 64px multiple —
  still a single tile covering the whole frame, but no longer demanding
  a 2048px tile for ~360p content. An explicit `tile_size` plan value is
  honored as a floor against the frame size.

### Added

- **Chunk-level DVD upscale progress.** The upscale stage reports the
  active chunk and step with each progress update. The run window shows
  "Upscaling: chunk 2 of 8 - AI upscaling", and the monitor `/status`
  endpoint exposes `chunk_current`, `chunk_total`, and `step`.

- **Application icon.** The Qt window loads a bundled `app_icon.svg`, and
  the Windows exe is built with an icon generated at build time from
  pure-Python drawing code (`packaging/build_icon.py`), so no binary
  `.ico` is checked into the repository.

### Validated

- Full local test suite: 251 passed.

## [0.2.23] - 2026-10-03

### Fixed

- **DVD AI upscale no longer writes visible tile grid artifacts.** Root
  cause: `realesrgan-ncnn-vulkan` selects its tile size automatically, and
  on Eric's first real DVD run the GPU driver/Vulkan heap split the small
  360p extraction frames into a 3x2 tile grid, leaving faint but visible
  seam lines in every written frame. The upscale stage now passes an
  explicit `-t 2048` (floored: any smaller or unset request becomes 2048),
  forcing a single tile per frame instead of driver-chosen spatial splits,
  and the mezzanine segment encoder now explicitly scales its output to
  the configured target height (720p, SAR 1) with lanczos instead of
  assuming the upscaled frames already match the target geometry.

### Added

- Regression tests for the forced tile size and the 720p mezzanine scale
  filter, including the not-found guard that rejected stubbed resolver
  resolutions in tests.

### Validated

- Full local test suite: 206 passed, 1 skipped.

## [0.2.21] - 2026-10-03

### Added

- **Reliable LAN monitor watcher.** Added `bluray-to-mp4 watch-monitor`, a
  stdlib-only watcher that reports `OK`, `STUCK`, `STALLED`, or `UNREACHABLE`
  with evidence from the remote monitor endpoint. This replaces the flaky
  JavaScript `fetch`-based checks used during validation.

### Changed

- `/status` and `/health` now include stage start time, last progress-change
  time, and a structured health verdict so stale `100%` states are detectable
  instead of looking healthy forever.

### Fixed

- `python -m bluray_to_mp4 watch-monitor ...` now routes through the CLI entry
  point instead of trying to launch the desktop UI first.

### Validated

- Full local test suite: 201 passed, 1 skipped.
- Windows packaged exe smoke test passed in GitHub Actions.

## [0.2.20] - 2026-10-03

### Fixed

- **DVD AI upscale is now available from the CLI.** The v0.2.19 engine and UI
  could plan and run DVD AI upscale, but unattended CLI runs had no
  `--dvd-upscale` switch, so release validation could not request the new
  stage without driving the GUI. `bluray-to-mp4 run` now accepts
  `--dvd-upscale off|720p|720p_anime` and forwards it into the same pipeline
  option the UI uses.

### Validated

- Full test suite: 195 passed, 1 skipped.
- Real Real-ESRGAN proof: SD source -> 720p AI-upscaled mezzanine -> MP4 with
  audio, embedded `mov_text` English subtitles, and retained SRT sidecar.

## [0.2.16] - 2026-10-02

### Added

- **DVD VobSub subtitle OCR.** The first real DVD run (2026-10-02)
  encoded fine but subtitle OCR failed: ffmpeg's `.sup` muxer is
  Blu-ray PGS-only and DVDs carry VobSub. The probed subtitle codec now
  chooses the extraction container - PGS (Blu-ray) still extracts to
  `.sup` exactly as before, VobSub (and unknown codecs) extract to a
  single-track MKV, the one container ffmpeg accepts for a
  `dvd_subtitle` copy and one Subtitle Edit reads for OCR directly -
  no new tool dependencies. The single-track intermediate guarantees
  Subtitle Edit OCRs exactly the stream the pipeline picked.
- **Subtitles are now embedded in the MP4.** After the OCR'd SRT is
  written as the usual sidecar, it is also muxed into the finished
  MP4 as a soft `mov_text` track - the only subtitle form every
  player (VLC, Plex, Jellyfin, Apple TV) reads from `.mp4`. The mux
  writes to a temp sibling and atomically replaces the MP4, so a
  failed embed can never destroy the finished encode: the run
  records a warning, the MP4 ships, and the editable sidecar `.srt`
  is always kept next to it. The embedded track is tagged `eng` when
  the rip's own stream tags confirm English, `und` otherwise
  (honest metadata for the v0.2.14 unconfirmed-language case).
- UI: total-remaining header pill; History: size + GB/hour; Setup:
  NVENC probe cache; drive-list timeout.

### Changed

- **DVD default size target is 1.0 GB/hour** (was 1.5; Eric's call,
  2026-10-02 15:05). Blu-ray stays at 2.0 GB/hour.

### Fixed

- **Fresh-rip accounting no longer depends on MakeMKV's summary
  line.** The end-of-run recovery of saved `.mkv` files was gated on
  the MSG:5004 "N titles saved" line, which the DVD run (validation
  run 9, 14:52) never emitted - a 100%-complete rip raised "finished
  without producing any files" while the file sat in the output
  directory. When no saved-file entries and no title failures were
  seen, `.mkv` files in the output dir are now recovered
  unconditionally (the run starts from a purged directory); an empty
  directory still raises the existing error.

## [0.2.15] - 2026-10-02

### Added

- **Review page: you can now override the recommended tracks.** Every
  title's Recommended audio and Recommended subtitles cells are now
  dropdowns (not fixed text): the audio dropdown lists every audio
  stream on the disc with novice-friendly labels (language, codec,
  channels, and a `[commentary]` marker on commentary tracks), and the
  subtitle dropdown leads with `(no subtitles)` and lists every
  subtitle stream with its classification (`[SDH]`, `[forced]`). The
  pre-selected default is still the recommendation, so the row reads
  exactly like before until you click. Picks are per-title, applied at
  Start time, and reset by a fresh scan. The pipeline honors them
  safely: an unknown stream id keeps the recommendation (never a
  silent encode), and picking `(no subtitles)` ships that title with
  no subtitles and no rip-time fallback shenanigans.
- **The v0.2.14 "English not confirmed" subtitle caveat is now
  visible on the Review page.** When the disc scan cannot confirm the
  subtitle language, the affected subtitle pick is marked with a
  warning sign, hovering the dropdown shows the full reasoning, and
  one muted line appears under the table telling you to check the
  dropdown.
- **Rip reuse now verifies the resolution, not just the duration**
  (backend, committed 94d9813). A leftover rip is reused only when
  its video width matches what the current disc's scan says it should
  be - a DVD pressing and a Blu-ray of the same movie no longer
  cross-match on runtime alone. Width-mismatched leftovers are
  deleted and re-ripped.

### Fixed

- **History table sits inside its card again** (Bob audit QW-1). The
  table was added to the page layout twice; the second add yanked it
  out of the History card, leaving a floating Refresh button over an
  orphaned table. The card keeps the table.
- **Dry-run warnings tell the truth about subtitle OCR** (Bob audit
  QW-2). Dry runs claimed "Subtitle OCR lands with roadmap #2" - OCR
  shipped in v0.2.2. The dry run now says OCR will run for the
  selected subtitle stream (with the v0.2.13 first-track fallback
  noted) and where the SRT sidecar will land.
- **Per-title overrides no longer shared by reference into the run
  plan** (Bob audit QW-5, folded in): editing an episode name while a
  run starts could crash the naming pass mid-iteration. The plan now
  holds its own copy.

## [0.2.14] - 2026-10-02

### Added

- **Total time remaining across ALL steps.** The Remaining area now
  forecasts the whole run: current stage's live ETA plus forecast
  seconds for every stage and job still ahead (rip ~BD+5min+read
  time, encode at ~6x realtime NVENC / 1.5x x265, OCR ~title/20).
  Each stage's forecast is replaced by its measured ETA the moment it
  starts, exactly as requested. On completion the area shows the
  total run time.

### Fixed

- **Review page: recommended subtitles column could be empty.** The
  recommender only auto-selected a subtitle it could positively
  confirm as English; when the scan data doesn't label the language
  (this disc's PGS tracks), nothing showed and there was nothing to
  select. The best non-commentary/descriptive subtitle is now always
  selected with an explicit "English not confirmed" caveat.

## [0.2.13] - 2026-10-02


### Added

- **Subtitles are now included by default even when the rip's stream
  list disagrees with the disc scan.** Validation run 7 shipped the
  first finished MP4 with no subtitles because the recommended
  subtitle id was missing from the ripped MKV's stream list; the run
  warned but shipped nothing. The pipeline now falls back to the
  first subtitle stream in the rip (MakeMKV preserves scan order), so
  English subs land as SRT sidecars by default - players can turn
  them off (Eric's preference: VLC handles it).

### Fixed

- History records carried duration 0 (TitleJob.duration_seconds was
  never populated from the scan) and the bare "subtitle missing"
  warning hid the real reason; the monitor history now shows the
  actual duration and the pipeline's specific warning text.

## [0.2.12] - 2026-10-02


### Added

- **A complete rip is no longer thrown away.** A cancelled or killed
  run can leave a fully ripped .mkv in the .rip workdir (Eric's
  "Mamma Mia! Here We Go Again" ripped 100% before run 5 died on the
  probe step). Before ripping each title, the pipeline now checks the
  expected file and duration-verifies it with ffprobe against the
  scanned title (within max(60 s, 5%)): a verified rip moves straight
  to encoding with no re-rip. Corrupt/partial leftovers fail the probe
  or the duration check and are deleted and re-ripped as before.

### Fixed

- **The run-start .rip purge now preserves expected job files.** The
  v0.2.8 purge deleted EVERYTHING in .rip; it now removes only files
  no planned job expects, so a reusable rip survives to the
  verification step while stale junk still gets cleaned. The
  anti-hang guarantee holds: an unverifiable expected file is deleted
  per-file before makemkvcon runs, so its interactive overwrite prompt
  is never triggered.

## [0.2.11] - 2026-10-02

### Fixed

- **Startup traceback in the console** ("AttributeError: 'MainWindow'
  object has no attribute 'stack'"). The v0.2.7 nav-signal fix set the
  initial sidebar row while the window was still being constructed -
  before the content stack existed - so the very first page-change
  signal crashed. The app recovered (the rip still ran), but printed
  the traceback and opened on Setup instead of Source. Initial row is
  now set after all pages are built, and _nav_changed guards against
  pre-construction calls.

## [0.2.10] - 2026-10-02


### Fixed

- **Run 5 post-mortem: rip finished 100%, then the run died on the
  probe step.** MakeMKV 2.0.0 emits "file added" lines for
  disc-internal files too (the 00803.mpls playlist); the parser
  treated any 3307 line as a saved output, so the playlist poisoned
  the output list and ffprobe failed with "No such file or
  directory". Only real .mkv outputs are now accepted (regression
  tested).
- **"Remaining: estimating ..." never became a countdown** (Eric
  report, validation run 5). MakeMKV emits PRGV duplicates ~2x/second;
  only fraction-advancing lines computed an ETA, so every duplicate
  sent eta=None and the UI erased the estimate it just had. The
  pipeline now carries the last real estimate forward across
  duplicates, the progress bar keeps its ETA on None ticks, and the
  Remaining label holds its countdown instead of flickering.

## [0.2.9] - 2026-10-02


### Added

- **The Encode page now always shows life.** Animated activity dots,
  an elapsed clock, and a time-remaining estimate run on every job.
  The stage progress bar pulses (indeterminate) until the first real
  progress event, so BD+ decryption at a legitimate 0% no longer looks
  frozen (validation-day request).

### Fixed

- **Rip percent/ETA actually stream now.** The v0.2.6 flag was wrong:
  official usage.txt says valid progress targets are -stdout/-stderr/
  -same and the default is NO progress output; our "--progress=-"
  (also misplaced after the command parameters) silently produced
  nothing. Now: --progress=-stdout before the mkv command.
- **Remote failures now carry the reason.** The monitor's error
  message previously named only which output failed, hiding why
  (validation run 4 failed at 12:11 with no diagnosable cause
  remotely). It now includes each error's reason.

## [0.2.8] - 2026-10-02


### Fixed

- **Stale rip intermediates could hang the whole run.** A cancelled or
  force-quit run left partial .mkv files in the .rip workdir; the next
  run's makemkvcon then sat forever on its interactive
  "already exists - overwrite?" prompt in robot mode, where nothing
  can answer it. The pipeline now purges the .rip workdir (ours alone)
  before every rip so each run starts clean (regression test included).

## [0.2.7] - 2026-10-02


### Fixed

- **Continue button was a silent no-op.** The sidebar row was set to
  Source before the nav signal was connected at startup, so the stack
  stayed on Setup while the sidebar said Source; clicking Continue set
  the row to a value it already had, no signal fired, and the page
  never moved. The signal now connects before the initial row, and
  Continue force-syncs the stack to Source regardless of sidebar state
  (regression test included).
- **Orphaned makemkvcon hangs are now self-explanatory.** A leftover
  MakeMKV process holding the drive made the Setup check time out with
  a cryptic message and the drive list spin forever. The Setup check
  now names the cause and the fix (end leftover makemkvcon in Task
  Manager), and a timed-out drive listing shows the same guidance
  instead of an endless "Looking for drives".

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
