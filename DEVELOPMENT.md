# Development notes and log

Design decisions, verified references, and the build history in one
place. Complements [README.md](README.md) (user-facing) and
[BUILD.md](BUILD.md) (packaging).

## Architecture

```
makemkvcon ──(robot mode)──> makemkv.py ──Disc/Title/StreamInfo──> recommend.py
                                │                                       │
                                └──> series.py (movie vs episodes)      │
                                                                       ▼
ui/ (PySide6) ──workers──> pipeline.py ──ffprobe.py──> encode.py ──ffmpeg──> MP4
       │                     │                                        │
       └───── monitor.py ◀───┴────────── subtitles.py ──Subtitle Edit──> .srt
```

- **Free engines orchestrated, nothing reinvented.** MakeMKV decrypts and
  rips; ffmpeg encodes and probes; Subtitle Edit + Tesseract OCRs
  subtitles. Our code is the glue: parsing, recommendations, naming,
  sizing, progress, and the UI.
- **Dependency-free core.** `makemkv.py`, `models.py`, `recommend.py`,
  `series.py`, `naming.py`, `encode.py` are stdlib-only, so the full chain
  (minus actually running external tools) is testable on any machine.
  External tools are injected as callables (`runner=`, `se_runner=`)
  throughout the tests.

## Verified references

The official MakeMKV developer documentation URL (makemkv.com/developers/)
404s as of 2026-10-01. All robot-mode wire formats were therefore verified
against Automatic Ripping Machine's production code
(`arm/ripper/makemkv.py`) and community notes (`arm_wiki/MakeMKV-Codes.md`):

- `DRV:index,status,999,disc-type,"drive name","disc label","path"` -
  status 2 = disc loaded; disc-type 1 = DVD, 12/28 = Blu-ray
- `TINFO` attributes: 8 = chapters, 9 = duration (`H:MM:SS`), 11 = bytes,
  27 = filename
- `SINFO` attributes: 1 = type ("Video"/"Audio"/"Subtitle" via code 6201/
  6202/6203), 11 = channels, 13 = bitrate, 19 = resolution, 21 = fps,
  28/29 = language code/name
- `MSG` codes: 5021/5055 = beta key expired, 5010 = disc open failure,
  5003 = title rip error, 3307 = file added, 5004/5037 = completion
  summary
- `PRGV:current,total,max` - progress on a constant 65536 scale
- Rip invocation: `makemkvcon -r mkv disc:<n> <tid> <outdir> --minlength=<s>`

Stream ordinals survive the rip: MakeMKV preserves per-type stream order
in the MKV, so the Nth scan-side audio stream maps to the Nth audio
stream in the file, resolved to absolute indices via ffprobe.

## Design decisions

**2 GB/hour via fixed average bitrate.** A size-per-hour budget converts
to a constant average bitrate (2 GB/h = ~4.4 Mbps total), independent of
runtime: total kbit/s = GB-per-hour x 8e6/1000/3600, minus audio
(448 kbps AAC 5.1) and 3% container slack. NVENC gets 1.45x VBV
headroom; x265 runs `slow` preset with 1.3x maxrate for tighter quality.

**No DVD upscaling.** SD sources carry no extra information; upscaling
only inflates the bitrate needed for identical perceived quality. DVDs
stay at native 480p/576p and get a 1.5 GB/hour budget.

**Subtitles as sidecar SRT.** MP4 cannot hold PGS (image) subtitles.
OCR to SRT matches the video filename so every player and media server
auto-loads it. Tesseract accuracy is ~95-99%, so cues are normalized
conservatively (timestamps repaired, text never rewritten) and sparse
output raises a review note. OCR failure downgrades to a warning: the
MP4 still lands.

**Series detection by duration clustering.** One title at 2.2x the
next-longest = movie; otherwise all titles >= 10 minutes are episodes
(sorted by MakeMKV title id, which follows disc authoring order in
practice; the start-episode offset handles multi-disc seasons).

**Windows binary discovery** (added in v0.1.1): installer dirs first
scanned after PATH because MakeMKV's installer adds no PATH entry -
found the hard way on the first real Windows machine.

**Monitor API is read-only by design.** It shares state with the UI via
in-process callbacks, never accepts requests that mutate anything, and
defaults to localhost. LAN mode requires an explicit config edit.

## Dev log

- **2026-10-01 (morning)** - Repo created; requirements gathered: 1080p
  MP4 ~2 GB/h, good audio, English subs, recommendation UI for
  novices, GPU support, DVD path (native SD recommended), TV series
  with per-episode naming. Free-tool landscape checked: MakeMKV +
  HandBrake/VidCoder/ARM cover pieces but none cover the whole flow
  with novice guidance -> build the glue, reuse the engines.
- **2026-10-01 (commit 084f25a)** - Core: robot-mode parser, models,
  recommendation engine, series detection, naming, encode builder.
  27 tests green against realistic fixtures.
- **2026-10-01 (commit 7eb91ae)** - Pipeline: ffprobe ordinal mapping,
  NVENC auto-detection, end-to-end runner with dry-run mode, CLI,
  README. 33 tests green.
- **2026-10-01 (commit c959096)** - Subtitles: SRT toolkit + Subtitle
  Edit OCR wrapper; OCR failures degrade to warnings. 42 tests green.
- **2026-10-01 (commit ebdda9f)** - PySide6 UI (4-page flow, worker
  threads), monitoring API, config persistence, dependency checker,
  PyInstaller spec. 48 tests green. Fixed a QThread shutdown crash
  caught by the offscreen UI smoke test.
- **2026-10-01 (commit 9b54df7, f197651)** - Windows CI: first build
  failed its own smoke test - PyInstaller ran `__main__.py` as a plain
  script so package-relative imports broke; `run_app.py` imports it as
  a module instead. Build green, exe smoke-tested on the runner.
- **2026-10-01 (release v0.1.0)** - exe shipped via GitHub release
  (Drive upload unavailable that day; gog auth timeouts).
- **2026-10-01 (release v0.1.1, commit 423516e)** - First real Windows
  machine reported `makemkvcon not found on PATH` despite a healthy
  install at `C:\Program Files (x86)\MakeMKV`. Added the three-tier
  discovery layer + regression test replaying the exact scenario.
  53 tests green. exe rebuilt and shipped.
- **2026-10-01 (docs sweep)** - README rewritten dependency-first;
  CHANGELOG, this file, and BUILD.md refreshed.
- **2026-10-01 (issue #6, v0.1.2)** - TMDB autofill: stdlib-only client
  (`tmdb.py`) with injectable fetchers for offline tests; pipeline uses
  it for episode titles (series) and year (movies) with fail-soft
  degradation; UI got a persisted key field and two fetch buttons on a
  background thread; CLI got `--tmdb-key` (plus `B2M_TMDB_KEY`).
  64 tests green. All roadmap issues now closed.

## Pending validation

Field test with a physical disc (paused 2026-10-01, no disc at hand):
scan a real title, verify rip -> ffprobe -> encode -> OCR chain, spot-
check subtitle sync and file size against the 2 GB/hour budget, and
watch the monitor API report during a real run.