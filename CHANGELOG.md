# Changelog

All notable changes to bluray-to-mp4. Dates are America/Chicago.

## [0.2.36] - 2026-10-19

### Fixed

- **Hotfix: the finalize-phase heartbeat shipped dead on every
  production path (v0.2.35's headline fix did not actually work).**
  The pipeline's gate block handed the raw single-tuple `on_progress`
  callback into `ProgressHeartbeat`, whose pings call it with three
  arguments — every tick raised `TypeError`, silently swallowed by the
  heartbeat's exception guard, so during the 20–65 minute frame-count
  gate the monitor still froze and false-reported stuck. The exact
  failure this release exists to kill. The gate block now wraps its
  callback in the same 3-arg→tuple adapter the concat call site one
  screen up already used. Found by Karen's v0.2.35 validation
  (verdict FAIL, one Critical) via fault-injection FI-7, which also
  proved the v0.2.35 suite could not lose this defect: 96/96 stayed
  green with the gate heartbeat deleted entirely, because no test
  drove the real gate block with a production-shaped callback. A new
  test does exactly that and fails on the dead shape (0 pings land).
- **The finalize step tuples said "chunk 163509 of 163509" instead of
  "chunk 57 of 57"** — they carried the stage's total frame count in
  the chunk fields; they now carry the actual chunk count, derived
  from the same chunking the mezzanine was built with.
- **The success-path provenance reason no longer claims "agrees" for a
  cross-check that snapped nothing.** The C.4 audit clause was gated on
  the mere presence of a container-format duration, so a run could
  read "format-duration cross-check 26.3025 agrees" while its own
  warning said the same figure matched no canonical rate. The "agrees"
  wording is now gated on the format side actually snapping to the
  same canonical token; non-agreement is recorded with honest wording.

### Docs

- Design doc §3.6's claim that gate-trip paths leave the resume workroot
  was false as shipped (the success rmtree runs before the pipeline's
  gates); the sentence is corrected — only an interruption mid-stage
  leaves the workroot, which is the resume path that matters.

## [0.2.35] - 2026-10-19

### Fixed

- **The upscale finalize phase now announces itself and stays alive in
  the monitor.** Concat, the frame-count gate, and the clock gate each
  emit a named monitor step, and a 15 s heartbeat keeps the status
  `updated_at` fresh through the full-decode gate (ffprobe -count_frames
  emits no progress; a 65-minute legitimate silence in today's completed
  Mamma Mia run read as five STALL false alarms and nearly cost a
  healthy ~5-GPU-hour run to a kill that had no per-chunk resume to fall
  back on). The health classifier now treats heartbeat-alive finalize as
  RUNNING and heartbeat-dead silence as STUCK - external watchers can
  tell legit quiet from a hang.
- **Interrupted DVD upscales now resume per chunk.** A re-entered run
  verifies each existing chunk segment (decode-count equals the chunk's
  expected frames, stamp rate equals the plan fps, file decodes) against
  a plan fingerprint and recomputes only the missing or mismatched
  chunks. The .rip purge preserves the .upscale tree when an upscale is
  planned; the rip-reuse lane is untouched; the final mezzanine and both
  gates are always rebuilt/re-run (concat is cheap; GPU hours are not).
- **A CFR-probe tool crash now fails open** to encoding the original
  rip like the upscale stage's other refusals, instead of failing the
  whole job (Karen's validation finding, pre-existing gap).
- **A true-23.976 disc whose audio extent under-reports by a few
  seconds can no longer snap to 24 and slip the clock gate.** The
  pre-flight now derives the rate twice - from the shipped audio
  stream's extent and from the container format duration - and REFUSES
  (fails open) when the two snap to different canonical rates. Successful
  runs record both figures in the run reasons for audit parity with
  rejection diagnostics.

### Added

- **The app's version is now visible everywhere a run is observed.** The
  main-window title and a header pill show the running build; the
  monitor's /status and /health payloads carry a `version` field so
  remote checks can verify build identity; and each History row records
  which build produced it. All surfaces read the single `__version__`
  source of truth (motivated by today's finalize-phase incident, where
  the running build could not be confirmed without asking the box).

## [0.2.34] - 2026-10-18

### Fixed

- **DVD AI upscale now derives the mezzanine stamp rate from decoded
  reality instead of container metadata.** The rip's decode-counted frame
  total is divided by the shipped audio stream's measured duration and
  snapped to a canonical rate (24000/1001, 24, 25, 30000/1001, 50,
  60000/1001) within 0.1%. Soft-telecined discs whose container lies
  uniformly (r_frame_rate == avg_frame_rate == 29.97 on a 23.976 film;
  the Mamma Mia HWA redo probe: 163,509 decoded frames over 6,818.4 s of
  audio = a true 23.976 clock, while both container fields claim 29.97)
  now stamp 23.976 and stay in sync - v0.2.33 would have reassembled the
  exact frame count at the lying 29.97 and shipped a ~20% desync. The
  v0.2.33 CFR pre-flight is retained as a diagnostic (VFR still
  refuses); the container's rate fields no longer set the stamp. A rate
  that snaps to nothing canonical is REFUSED before any GPU work,
  failing open to the original rip exactly like the v0.2.33 VFR
  refusal.
- **Restored a clock gate next to the frame-count gate.**
  `verify_mezzanine_clock` checks the mezzanine's container duration
  against the rip's selected-audio duration within 0.1% (floor 2 s);
  a mismatch fails open, retains the rejected mezzanine, and the
  diagnostic now records the decoded-truth rate (raw + snapped + chosen
  stamp), the audio duration and its source, the container's claimed
  rates side by side, and the mezzanine's measured duration with the
  gate tolerance. This is not the old `verify_mezzanine_duration`
  (which compared against the retired title-scan integer): it probes
  the mezzanine against the audio stream the encode actually ships.

## [0.2.33] - 2026-10-18

### Fixed

- **DVD AI upscale now chunks by exact frame index, not time windows.**
  The rip's frame count is probed once (decode-counted, not metadata)
  and each chunk extracts frames [i*F, (i+1)*F) via ffmpeg's select
  filter. Accumulated duration drift — the failure that burned six
  GPU-hours on Mamma Mia HWA and still shipped the original rip — is
  impossible by construction.
- **The upscale gate now requires exact frame-count equality** between
  mezzanine and rip (was: duration within 0.5 s + 0.01%). A mismatch
  fails open to the original rip as before, but the rejected mezzanine is
  now retained next to a written diagnostic (expected vs actual counts,
  fps probe values) so future drift is measurable from artifacts.
- **Variable-frame-rate rips are refused before any GPU work.** A
  pre-flight check compares r_frame_rate vs avg_frame_rate; a true VFR
  rip fails open in seconds with an honest warning instead of after the
  upscale runs.

## [0.2.32] - 2026-10-08

### Fixed

- **The final MP4 filename now exactly matches the movie title shown
  in Review.** The run worker previously rescanned the disc after
  Review and rebuilt the filename from the raw disc volume label, so
  a disc labeled `MAMMA_MIA` produced `MAMMA_MIA (2019).mp4`
  instead of `Mamma Mia! Here We Go Yet Again (2019).mp4`. The
  reviewed title now flows through the run plan into the TMDB
  lookup and the final encode path.
- **Regression tests cover each handoff boundary.** The UI run plan
  preserves the reviewed title, the worker passes it through to the
  pipeline, and the final output path exactly matches the reviewed
  name.

## [0.2.31] - 2026-10-05

### Fixed

- **DVD AI-upscale audio/video drift is now rejected before the final
  encode.** The upscale plan prefers FFmpeg's timestamp-derived
  `avg_frame_rate` over the nominal `r_frame_rate`, preventing a slightly
  fast nominal clock from accumulating visible sync error across a feature.
- **The mezzanine duration gate now catches sub-percent feature-length
  drift.** Its tolerance is tightened from `max(5 seconds, 2%)` to
  `max(0.5 seconds, 0.01%)`. The measured Mamma Mia failure shape
  (15.050 seconds over 1:53:39) is pinned by a regression test. If the gate
  trips, the existing fail-open path discards the upscale and produces a
  synchronized original-resolution encode instead of shipping a bad file.

## [0.2.30] - 2026-10-04

### Added

- **OCR quality reporting (honest signal, never a gate).** Eric
  reported the run-12/13 (GUI-wizard era) OCR text was "nowhere near
  accurate"; the v0.2.27 unattended flow removes the babysitting but
  Tesseract can still misread a disc's VobSub bitmaps (stylized
  musical fonts are exactly its weak spot), and tonight's run would
  have shipped the poor text silently. Every OCR run now appends a
  deterministic, data-driven quality line to its notes:
  `OCR quality: X% of Y cues clean (Z cues contain non-English
  characters, W empty)`. "Clean" is a documented conservative
  character contract (ASCII letters/digits/spaces, basic English
  punctuation, common accented Latin-1 letters) - pipes, the cent
  sign, curly quotes, and angle brackets all count as garbage, which
  is the shape Tesseract's misreads of stylized subtitle fonts take.
  Counts are taken before normalization drops empty cues so the three
  numbers always add up. Pure reporting: a garbage-heavy result still
  ships (fail-open stands), but the run now says so with numbers.

- **OCR diagnosis retention kit.** Re-ripping a disc to re-tune OCR
  accuracy is expensive, so every OCR stage - success path AND the
  fail-open path where OCR failed but the MP4 ships - now retains its
  inputs in `outdir/<output-stem>.ocr/`: the extracted subtitle track
  that was fed to seconv (the single-track MKV for VobSub, the .sup
  for PGS) and the raw pass-1 SRT as `<stem>.raw.srt`. That is enough
  to re-run seconv offline with different engines/settings and diff
  the output, no disc re-rip needed. The track copy is size-capped at
  150 MB (skipped with an honest note above that); the tiny raw SRT
  is always kept. A subfolder on purpose: a bare raw .srt sibling
  would make every player auto-load the uncorrected pass-1 text.

### Fixed

- **Auto-resolved cue count pairs cues by timing.** Karen's
  validation nit: the count paired raw and fixed cues positionally
  with `zip()`, but pass 2 merges and splits cues, so the pairing
  drifted one-for-one and undercounted exactly when the auto-fix pass
  had done the most work. Fixed cues are now paired to raw cues by
  start timestamp (raw texts stored per start_ms; a fixed cue counts
  as changed when its text is absent at its start_ms, or its start_ms
  was created by a split). Merged/split cue shapes that the old logic
  miscounted are pinned by tests.

## [0.2.29] - 2026-10-04

### Fixed

Karen's independent validation of v0.2.28 (run a429efb0, verdict
PASS with findings) surfaced four minor defects; each ships with a
regression test.

- **Stale raw OCR SRT could ship under a success path.** A killed
  prior OCR in the same workdir leaves `b2m_ocr_raw.srt` behind; a
  seconv pass-1 that exits 0 without writing then passed the
  "raw SRT exists" check and shipped that stale file - potentially
  a different title's subtitles - as a clean success. The wrapper
  now deletes a stale `b2m_ocr_raw.srt` before pass 1, symmetric
  with the fixed-SRT cleanup that already runs before pass 2.

- **A truncated auto-fix SRT no longer loses the raw OCR.** If
  pass-2's "fixed" file exists but does not parse (a partial write
  from a killed run), the OCR stage previously raised and the
  pipeline's fail-open shipped the MP4 with NO subtitles at all.
  It now falls back to the raw SRT instead and records an honest
  note that the shipped sidecar skipped glyph corrections.

- **Monitor port walk could crash startup above 65535.** A
  configured port near the top of the range (>= 65526 with the
  default 10-port fallback) made the walk offer candidates above
  65535; binding there raises OverflowError, which is not an
  OSError and escaped both the walk's handler and the UI's
  `except OSError`. The walk is clamped to 65535 and the first
  real bind error surfaces instead.

- **The upscale duration gate could pass with an unknown rip
  duration.** When the rip could not be probed AND no title
  duration was available (rip duration effectively <= 0), the
  gate's 5-second tolerance floor would accept ANY mezzanine. The
  gate now refuses an unknown rip duration exactly like an
  unprobeable mezzanine, so the existing fail-open encodes the
  original rip with a warning.

### Docs

- **CHANGELOG [0.2.28] zip-growth bullet corrected:** the release
  zip grows ~31 MB (351,615,113 B - 320,608,925 B = 31,006,188 B),
  while the bundled fp16 model is 33.4 MB (33,434,888 B) - the
  old "~34 MB" bullet conflated the two.

- **README honest-validation block refreshed to the 2026-10-04
  state:** runs 12-13 happened (OCR required babysitting -
  root-caused and replaced with headless seconv + Tesseract in
  v0.2.27; the audio-desync root cause is fixed via the real-fps
  probe and mezzanine duration gate), x2plus is the live-action
  profile since v0.2.28, and the remaining field-unverified item
  is Windows-native OCR end-to-end.

## [0.2.28] - 2026-10-04

### Changed

- **Live-action 720p DVD upscale profile now runs RealESRGAN_x2plus**
  (the bundled `realesrgan-x2plus`) instead of `realesr-animevideov3`.
  x2plus is a TRUE native-2x model: official general-purpose
  RealESRGAN_x2plus weights (Real-ESRGAN release v0.2.1) converted
  through the official conversion chain (pytorch→onnx→ncnn,
  ncnnoptimize fp16), with one documented, machine-precision-exact
  deviation - the 2022 engine binary lacks the Reorg layer for the
  model's pixel_unshuffle(2) head, so it is algebraically folded into
  the first convolution (float64 max diff 2.2e-15). The conversion was
  verified on the actual realesrgan-ncnn-vulkan engine: exact
  1280×720 from 640×360 input, full-image integrity, and 100.000% of
  pixels within ±1/255 of the PyTorch reference (fp32 CPU ncnn gold;
  the Vulkan fp16 error class matches the stock official x4plus
  control). Eric eyeballed the verified output frames and approved
  the switch (2026-10-04, "I think it looks good"). This is NOT the
  v0.2.25 corruption case, where the native-4x realesrgan-x4plus was
  misrun at -s 2 and corrupted frames; x2plus at -s 2 is its native
  mode, so the forced single-tile strategy and 640×360→1280×720
  geometry are unchanged. The anime profile keeps
  `realesr-animevideov3` at native 2x, as since v0.2.26.

- **The release zip grows ~31 MB** (measured delta: 351,615,113 B -
  320,608,925 B = 31,006,188 B; the bundled fp16 model is 33.4 MB,
  33,434,888 B). x2plus is a 23-block RRDBNet versus animevideov3's
  compact VGG-style video net, so the live-action profile is slower
  per frame (a ~7.5–8× per-frame ratio was measured on the
  verification host's llvmpipe CPU device only; the 3090 Ti ratio
  is expected to hold the same ordering but will be measured on the
  first real run). `resolve_realesrgan_models`
  now requires `realesrgan-x2plus.param`/`.bin`, so a broken bundle
  fails loudly at stage setup instead of mid-disc.

## [0.2.27] - 2026-10-04

### Fixed

- **Subtitle OCR is now fully unattended** (Eric's 2026-10-02 zero-
  interaction criterion - "end-to-end run with ZERO human
  interaction, including OCR" - which was still unmet on the morning
  of 2026-10-04, when Eric had to click Start OCR and hand-verify
  glyphs). Root cause: the packaged app bundled Subtitle Edit 5.2.0's
  GUI, whose 5.x rewrite dropped the `/convert` command-line mode -
  so every "headless" OCR invocation actually opened the interactive
  OCR wizard and waited for a human. The bundle now ships seconv
  (Subtitle Edit's own headless converter, same 5.2.0 core) plus a
  bundled Tesseract 5.5.3 with its English model, and the wrapper
  refuses the GUI binary outright. Both PGS (Blu-ray) and VobSub
  (DVD) OCR run as two non-interactive seconv passes: raw Tesseract
  OCR, then a headless OCR-fix pass that auto-resolves unknown glyphs
  with best-guess replacements using dictionaries bundled inside
  seconv (no training, no dialogs). The run records an honest warning
  naming the auto-resolved cue count - a silent pass was the run-12
  defect - and a hard OCR failure still takes the existing fail-open
  path (MP4 ships without subtitles, with a warning). The .srt
  sidecar remains the editable truth.

- **DVD AI-upscale audio/video desync from the hardcoded 29.97 fps
  stamp** (run 13, 2026-10-04 morning: the full v0.2.26 upscaled MP4
  progressively lost sync). Root cause: the mezzanine writer stamps
  every reassembled chunk with `UpscalePlan.fps`, whose hardcoded
  default was 30000/1001 - but the pipeline constructed the plan
  without probing the rip's real frame rate, and MakeMKV strips
  pulldown from film DVDs, so a 23.976 fps rip was reassembled at
  29.97 and every concatenated segment drifted ~20%. The pipeline
  now probes the rip's real frame rate (ffprobe `r_frame_rate`,
  `avg_frame_rate` fallback) after `probe_streams` and passes it into
  `UpscalePlan.fps`, so extraction, chunk timing, and mezzanine
  reassembly share the rip's true clock; an unparsable rate falls
  back to the 30000/1001 default with an honest warning. A new
  duration sanity gate probes the finished mezzanine and - when it
  differs from the rip by more than max(5 seconds, 2%) - raises
  `UpscaleError` naming both durations, so the existing fail-open
  path encodes the ORIGINAL rip with an honest warning instead of a
  desynced mezzanine silently reaching the encode.

## [0.2.26] - 2026-10-03

### Fixed

- **LAN monitor falls forward when Windows reserves the configured port.**
  If the preferred monitor port raises a bind error such as WinError
  10013, startup now tries nearby ports and persists the successful port
  so later launches do not repeat the failure.

- **DVD AI upscale restores forced single-tile sizing.** v0.2.25
  replaced v0.2.24's extent-derived tile argument with hardcoded `-t 0`
  (model/driver auto tiling), which reintroduced the v0.2.23 tile-seam
  artifact on Eric's 3090 Ti: a faint full-width horizontal seam near
  the bottom of every frame. The Real-ESRGAN chunk command now derives
  `-t` from the measured input frame extent plus a 64px safety margin
  (rounded up to a 64px multiple; explicit `tile_size` honored as a
  floor), forcing a single tile per frame so the Vulkan driver cannot
  split frames. Covered by a regression test that fails on `-t 0`.

### Changed

- **DVD 720p upscale now runs realesr-animevideov3 at native 2x.** The
  live-action 720p profile previously ran realesrgan-x4plus at 4x and
  lanczos-downsampled the mezzanine to 720p. realesrgan-x4plus
  corrupts this frame path at `-s 2` (the v0.2.25 lesson), so the
  profile now uses the bundled true-2x video model realesr-animevideov3
  at native 2x: a 640x360 square-pixel extraction lands directly at
  1280x720 with no 4x round trip and no downsample - roughly 4-8x
  faster on the 3090 Ti. The 720p and 720p (anime) profiles now share
  the same engine path.

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
