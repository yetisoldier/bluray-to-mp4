# Scoping assessment: standalone SD-to-720p file upscaler (sibling project)

Status: assessment only (no code, no repo creation). Author: architect
subagent, 2026-10-02. Requested by Eric 2026-10-02 21:59: could a
DIFFERENT project take SD video FILES in various formats (AVI, MP4,
MOV, etc.) of unknown original resolution and upscale them to 720p
HEVC MP4? His real collection: a few obscure 1990s-2000s movies to
improve. This doc says what that project is, what it reuses from
bluray-to-mp4's v0.2.19 design (docs/designs/v0.2.19-dvd-ai-upscale.md,
final), and what v0.2.19 must do differently so the reuse happens.

---

## 1. VERDICT: separate project, not a fork, not an input mode

**Recommendation: a separate project that imports bluray-to-mp4's
upscale core. Not a fork; not a new input mode inside the disc app.**

1. **Input mode inside bluray-to-mp4: rejected.** The disc app's
   assumptions run deep: scan -> title list -> rip (MakeMKV) -> OCR
   (Subtitle Edit) -> encode, every stage anchored to "a disc was
   inserted." A file mode forks the Review/Queue pages into two
   near-identical products in one exe, or litters every stage with
   `if files:`. The app is novice-friendly because it does one narrow
   thing.
2. **Fork: rejected.** It duplicates the v0.2.19 upscale stage and
   diverges the first time v0.2.20 fixes a bug in it, exactly at the
   hardest, most valuable code.
3. **Separate project over a shared importable core: recommended.**
   Two products share an *engine*, not a *product*: one is "insert
   disc, get MP4s," the other is "drop files, get better MP4s."
   Different front ends, one core. Coupling cost is one pinned
   dependency (git submodule or path dep while both stay personal),
   not a monorepo.

**Names:** project **VidLift** (working title, Eric can veto); repo
`yetisoldier/vidlift` (private, yetisoldier org); publisher under the
existing **yetiwurks** convention, same umbrella as bluray-to-mp4.

---

## 2. THE REUSE SEAM (binding recommendation for v0.2.19)

The one thing here that affects in-flight work: **v0.2.19's stage and
helpers must be built as a decoupled, importable core whose public
surface never mentions discs, rips, drives, titles, or MakeMKV.** If
that holds, the file tool is a shell job; if not, it rewrites v0.2.19.

### 2.1 Core contract (what the file tool imports)

Four pieces, all already planned in the v0.2.19 design; these seam
rules are what make them reusable:

1. **Engine adapter** (`upscale/engine.py`): one `UpscaleEngine`
   interface, three lanes behind it (bundled ncnn-vulkan fallback;
   RVE-via-CLI TensorRT primary if the benchmark picks it; own-harness
   contingency). Interface is exactly `detect() -> Engine | None` and
   `run(in_chunk_dir, out_chunk_dir, model, scale, cancel) -> frames_done`.
   Chunk-dir in/out is the lane contract per v0.2.19 Section 4.2.
2. **Prepare-filter builder** (`upscale/prepare.py`): probed stream
   record + file path in; ffmpeg filter chain + classified output fps
   out. This is where v0.2.19 Section 3.3's three-way classification
   (progressive / fieldmatch+decimate IVTC / genuine interlace at
   field rate) and Section 3.4's `setsar=1` geometry live. **Rule:
   input is a probed stream + path, never a Disc/Title object.** The
   fixed DVD geometry table generalizes to
   `displayed_size(w, h, sar) -> (dw, dh)`, with the four DVD rows as
   the non-square-SAR special case (Section 3.2 below).
3. **Mezzanine writer** (`upscale/mezzanine.py`): chunk dirs + fps in,
   video-only mezzanine MKV (FFV1/HEVC) out, concat across chunks.
   No knowledge of what produced the frames.
4. **Encode planner** (today's `encode.py`): `EncodePlan`,
   `build_encode_args`, `estimate_video_bitrate_kbps` are already
   media-agnostic apart from the `media_type` string and two default
   constants; the v0.2.18 GB/hour contract and v0.2.19's 1.5 pairing
   carry straight to 720p files. **Rule:** keep v0.2.19 Section 4.2's
   `video_input_path` encoding seam file-shaped; no disc constants
   leak out.

### 2.2 The one-stage entry point

v0.2.19's stage runner should take the shape

```
run_upscale(input_file_path, plan: UpscalePlan, cancel) -> mezzanine_path
```

where `input_file_path` is "a prepared progressive square-pixel source
file" (ripped MKV for bluray-to-mp4; the user's file for VidLift) and
`UpscalePlan` carries model, scale, target geometry, fps, chunking,
temp dir, progress callback. v0.2.19 Section 4.2 currently says the
stage decodes "the ripped MKV" - the only binding change this scoping
asks for is that the decode input is a caller-supplied path, and the
caller supplies the prepare decisions Section 3.3's classifier made.

### 2.3 What the core must NOT own

Disc scanning/enumeration, MakeMKV, drive detection; naming/TMDB
(VidLift names outputs from the input filename); subtitle OCR (file
containers carry text subs; Section 3.3); UI dropdowns, slider
pairing, persistence (per product). Keep those out of `upscale/` and
the seam holds. That is the entire binding ask.

---

## 3. INPUT REALITY for a file-based tool

### 3.1 Format matrix

| container | typical codec (90s/2000s) | notes |
|---|---|---|
| .avi | XviD, DivX 3/4/5 | the ~700 MB rip; MPEG-4 ASP, 23.976 or 25 fps |
| .mpg/.mpeg/.vob | MPEG-1/MPEG-2 | DVD rips/captures; needs the interlace/telecine classifier |
| .mkv | anything | already the internal intermediate |
| .mp4/.m4v | MPEG-4 ASP or early h264 | iTunes-era rips |
| .mov | Sorenson, MPEG-4, ProRes | Mac-era encodes |
| .wmv | WMV7/8/9 (VC-1) | decodes fine; often low bitrate |
| .flv | Sorenson/VP6/early h264 | flash-era; audio often 44.1 kHz MP3 |

All decodable by the bundled ffmpeg. This is a test-matrix line item,
not an engineering risk.

### 3.2 "Unknown resolution" is a non-problem; say so

Every decoder reports real width/height, SAR/DAR, both frame rates,
and `field_order`; `ffprobe.py` already parses most of this. VidLift
extends the probe for SAR and both rates, then classifies exactly per
v0.2.19 Section 3.3. Detection table, no guessing anywhere:

- width x height decides candidacy at all: upscale only when displayed
  height <= 576 after square-pixel normalization. HD files get an
  "already HD" verdict in the pre-flight report, not an upscale.
- `sample_aspect_ratio` drives displayed-geometry math
  (`displayed_size(w, h, sar)`), then `setsar=1` per v0.2.19 3.4;
  anamorphic AVI/DivX rips exist and are handled by the same rule.
- `field_order` + `r_frame_rate` vs `avg_frame_rate` drive the
  progressive / IVTC-to-23.976 / genuine-interlace-at-field-rate call,
  verbatim from v0.2.19 3.3.

The tool never invents "original resolution"; it reads what the file
declares and spot-checks one 60-second sample for interlace.

### 3.3 Audio and subtitles

- **Audio: normalize to AAC**, mirroring encode.py's channel-count
  table (`AUDIO_KBPS` unchanged): 5.1 -> 448k transcode, stereo 192k,
  mono 128k. PCM-in-MOV and MP3-in-AVI are ordinary decode+encode.
- **Subtitles: pass text subs through.** mkv/mp4 inputs carry SRT/ASS
  natively; map to mov_text in the MP4 (or keep SRT sidecars via one
  later checkbox). VobSub-in-MKV (image subs) is the one overlap with
  the disc app's OCR stage: v1 detects image subs, lists them as "not
  carried" in the report, and skips. No OCR engine in v1.
- **Chapters: drop.** Report notes it; no promise.

### 3.4 Honest quality expectations, per source class

Eric should read this against his own shelf:

- **Class A: proper DVD MPEG-2 rips (vob/mkv, 4-9 GB per movie).**
  Best case, exactly v0.2.19's target: MPEG-2 blocking and mosquito
  noise are ESRGAN's best input; live-action verdict carries over:
  "good, not transformative," the best 720p that source can give.
- **Class B: ~700 MB XviD/DivX AVI (the expected common case for
  obscure 90s/2000s movies).** Already lossy-compressed to ~1/6th of
  the DVD: detail the DVD had is gone, replaced by DCT blocking,
  ringing on text, smeared grain. AI upscale helps - ESRGAN's denoise
  cleans blocking visibly, 2x-then-lanczos still sharpens real edges -
  but **it cannot recover detail never captured in the file**. Expect
  "clearly cleaner on a TV," not "looks remastered." A 350 MB dark
  rip may still be a NO for that title; the pre-flight report
  (Section 4) exists so that is learned from a 3-minute sample, not a
  6-hour encode.
- **Class C: WMV/FLV/early MP4.** Very low bitrates; upscale helps
  least. Best-effort, watch the sample first.
- **Restoration-first note (all classes):** on heavily compressed
  input, denoise/deblock-forward routing (ESRGAN denoise strength, or
  light hqdn3d before the model) beats raw x4plus. VidLift inherits
  whatever model routing the v0.2.19 benchmark blesses; no second
  model-selection effort.
- **PAL speedup, the common 90s/2000s case:** many European rips of
  US films are 25.000 fps and play ~4% fast with raised pitch.
  Detection: progressive 25 fps + film DAR + runtime shorter than the
  known cut. v1: **warn in the report and process as-is.** Retiming
  to 23.976 with pitch correction is a v2 rabbit hole; do not couple
  it to launch.

### 3.5 Output geometry

720p-frame HEVC MP4, anamorphic-safe: 16:9 -> 1280x720, 4:3 ->
960x720, via the same `-2:720` lanczos rule as v0.2.19 3.4, applied
to the displayed size from 3.2.

---

## 4. PRODUCT SHAPE

1. **Batch queue first.** Drag-drop files or pick a folder; queue in
   drop order with per-file status. No library, no watch list, no
   metadata editor - the queue is the product.
2. **Per-file pre-flight report before committing hours.** On drop:
   probe (sub-second), classify (Class A/B/C; progressive/telecine/
   interlaced; PAL-speed flag; resolution+SAR; channels; text subs),
   run ONE tiny timed upscale sample (~3 s of video through the
   detected engine) to measure real fps on this box, then show
   projected wall clock, projected output size at the GB/hour target,
   and the honest expectation string. Start per file or Start All.
   This is v0.2.19's dry-run honesty block promoted to the front
   door, because files have no "insert disc" moment of commitment.
3. **Size target: the v0.2.18 GB/hour contract at 720p.** Same spin
   and bitrate math (`estimate_video_bitrate_kbps` unchanged);
   default 1.5 GB/hour per Eric's pairing rule, per-run in-memory.
   Most movies land ~1.7-2.6 GB - the sensible shelf size for "better
   than my old AVI."
4. **Watch folder: optional v2.** Timer-scanned folder queuing with
   last-used settings. Cheap later (a directory iterator feeding the
   same queue), correctly excluded now.
5. **CLI for unattended runs, day one.** The zero-touch rule from
   run 12 applies with full force: a batch of 6-hour jobs is where a
   GUI babysitter fails at 3 AM. Shape:
   `vidlift-cli --target 720p --gbph 1.5 FILE...` / `--folder DIR`,
   same engine/config resolution as the GUI, per-file exit codes,
   stderr summary. The CLI also runs on the Linux box with no Qt,
   keeping the core honest about the Section 2 seam.

---

## 5. SEQUENCING + EFFORT

**Prerequisite: v0.2.19 lands in bluray-to-mp4** (benchmark afternoon
on Eric's box, engine selected, stage shipped, one real DVD
validated). VidLift before that re-runs the benchmark twice; after it,
Section 1.1's estimate ranges are measured numbers.

After v0.2.19, assuming the Section 2 seam held, **~3 focused
sessions**:

1. **Core consumption + probe/classify:** repo skeleton (same
   PySide6/pytest toolchain), pinned import of `upscale/` +
   `encode.py`, file ffprobe wrapper (SAR/DAR/rates, Class A/B/C, PAL
   flag), unit tests over recorded ffprobe JSON fixtures. No real
   videos needed for these.
2. **Batch queue UI + pre-flight report:** drop/pick, report cards
   (probe + 3-second engine sample + projected time/size), Start/
   Start All, cancel between files and mid-file (inherits the
   engine's cancel).
3. **CLI + end-to-end validation:** `vidlift-cli` with per-file exit
   codes; pipeline correctness on the Linux box; one real full-movie
   run on Eric's Windows box for wall-clock truth against the
   report's projection.

If the Section 2 seam does NOT hold, session 1 becomes "rewrite the
stage" and the estimate roughly doubles - the strongest argument for
building v0.2.19 right.

**Local (Linux/minipc) vs Eric's Windows box:** sessions 1-2 and CLI
correctness are fully testable locally (probe fixtures are JSON; the
engine is faked behind the adapter; queue/report logic is pure
Python). What needs Eric's box: 3090 Ti throughput truth for the
report's projections (the 3-second sample), TV playback acceptance,
and his real files' weirdness. None of that gates sessions 1-2.

**Sample-file ask (small, high value):** 2-3 representatives of his
actual collection - one ~700 MB XviD/DivX AVI (Class B, the expected
common case), one proper DVD MPEG-2 rip (vob or mkv, Class A), one
MOV or odd-container file. Trimmed clips of a few hundred MB are
fine; full movies welcome but not required to start.

---

## 6. DECISION FOR ERIC

**GO (conditional).** The file upscaler is a viable product that
shares v0.2.19's engine instead of competing with it, and its hardest
problem is already designed and benchmark-planned. Conditions: v0.2.19
is built to the Section 2 importable-core contract, and sequencing
stays v0.2.19 first, VidLift after, in `yetisoldier/vidlift`
(private).

**To firm this up, from Eric:**
1. The 2-3 sample files (one XviD AVI, one DVD MPEG-2 rip, one MOV or
   odd container; trimmed clips OK).
2. One-line GO confirmation and the project name (VidLift / veto + his
   pick).
3. His read on when the v0.2.19 benchmark afternoon happens -
   VidLift session 1 starts after it.
