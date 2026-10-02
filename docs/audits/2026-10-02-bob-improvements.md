# Improvement audit — 2026-10-02 (Bob, while Eric is out)

Scope: analysis only. Nothing here is implemented. Proposals are sorted into
QUICK WINS (safe to land in v0.2.16 tonight alongside the committed work),
NEXT RELEASE (v0.2.17 candidates, need Eric's OK), and NOT YET (right idea,
wrong time).

Method: full read of README.md, DEVELOPMENT.md, CHANGELOG.md, all of
`bluray_to_mp4/` (5,109 lines incl. UI), `tests/` (92 tests), and the three
committed design docs (v0.2.15 review overrides, v0.2.16 VobSub OCR +
embedded subs). The committed v0.2.15/v0.2.16 scopes are treated as fixed;
nothing below re-proposes them.

## Tonight's risk check (read first)

Nothing found that would break tonight's v0.2.16 test run. Two cautions for
whoever lands quick wins:

1. **QW-3 touches `make_runner` in makemkv.py** — the same runner used by
   scan and rip. It MUST stay scoped: the new kill-on-timeout only fires for
   callers that pass a `timeout`, and only the UI drive-list call starts
   passing one. Scan and rip keep `timeout=None` (byte-identical behavior).
2. **QW-5's one-line change sits exactly where Forge's v0.2.15 adds
   `plan["stream_overrides"]`** (`_start_run`). Fold it into that edit as a
   9-character wrap (`dict(...)`) rather than a separate commit to avoid a
   needless conflict.

Do NOT land NR-6 (overwrite confirmation) tonight: Eric re-runs the same
disc repeatedly during validation, and a new "file exists — overwrite?"
dialog would appear constantly mid-test.

---

## QUICK WINS (v0.2.16-compatible, ranked)

### QW-1 — History page: table is added to the layout twice (one-line delete)

- **What.** `ui/main_window.py:722` does `cl.addWidget(self.history_table)`
  inside the card, then line 729 does `layout.addWidget(self.history_table)`
  again (followed by a stray unused `history_label`). The second `addWidget`
  moves the table OUT of the card frame to the page bottom, so the History
  card renders as a Refresh button floating over an orphaned table. This is
  the page Eric stares at after every validation run.
- **Why.** Layout bug on a primary page; cosmetic but confusing ("is the
  history broken?") during a validation day where History is the scoreboard.
- **Effort.** XS — delete line 729 (and the dead `history_label` next to it).
- **Risk.** None. The card keeps the table; no behavior change.
- **Verify.** `pytest tests/test_ui.py` (existing construction tests cover
  the page). Karen: open History — table sits inside the card.

### QW-2 — Kill the stale dry-run lie (one line, pipeline.py:362)

- **What.** Dry runs still warn `"Subtitle OCR lands with roadmap #2; SRT
  path reserved: ..."` for every job — OCR shipped in v0.2.1. The CLI prints
  a claim that is two weeks stale.
- **Why.** Trust in warnings is everything on a validation day; a warning
  that is verifiably false teaches the user to ignore warnings.
- **Effort.** XS — rewrite to `"SRT sidecar will be written to: <path>"` (it
  is still useful to confirm where the SRT will land) or drop entirely.
- **Risk.** None — dry-run warning text only; the real run never reads it.
- **Verify.** `tests/test_pipeline.py` dry-run test asserts the warning
  shape; update the one assertion. `python -m bluray_to_mp4 run 0 --out X
  --dry-run` (no disc needed if a fixture runner is used in tests).

### QW-3 — Drive list can still hang forever; make the v0.2.7 guidance reachable

- **What.** v0.2.7 added excellent orphan-makemkvcon guidance text, but the
  UI drive list calls `find_drives()` with **no timeout**
  (`workers.py:41`). `make_runner(timeout=None)` blocks in `proc.wait()`
  indefinitely, so when an orphan makemkvcon holds the drive, "Looking for
  drives ..." spins forever — and the "timed out → show the fix" branch in
  `_scan_failed` can never fire on this path. The guidance exists but is
  unreachable exactly where it is needed most (this happened twice today).
- **Why.** This is today's #1 confusion vector. Eric's recovery (Task
  Manager → kill makemkvcon) is right; the app just never tells him.
- **Fix shape.** Two tiny, scoped changes:
  1. `makemkv.py::make_runner`: wrap the `proc.wait(timeout=timeout)` so a
     `TimeoutExpired` kills the child (`proc.kill(); proc.wait()`) before
     propagating — today a timeout would also LEAK the hung makemkvcon,
     creating a second orphan.
  2. `workers.py::ScanWorker.run` drives-mode only:
     `find_drives(timeout=60)`. Scan and rip callers keep `timeout=None`.
     `TimeoutExpired`'s message ("Command ... timed out after 60 seconds")
     already contains "timed out", so the existing `_scan_failed` mapping to
     the orphan guidance lights up with zero UI changes.
- **Effort.** S (~4 lines + 2 tests).
- **Risk.** Small and fenced: behavior changes only when a caller passes
  `timeout`, and only the drive list does. 60 s is generous (DRV enumeration
  does not spin up the disc; the deps probe uses 30 s for a bigger query).
- **Verify.**
  - New `tests/test_makemkv.py`: monkeypatched Popen whose
    `wait(timeout=...)` raises `TimeoutExpired` → assert `kill()` was called
    and the exception propagates.
  - New `tests/test_ui.py`: feed `_scan_failed` a "timed out after 60"
    string → assert the orphan-MakeMKV guidance text appears.
  - Karen: with no orphan, Refresh works as before (the common case is
    unchanged).

### QW-4 — Cache the Setup NVENC probe in `settings.extras` (tool-version stamped)

- **What.** Every launch (and every Re-check) runs `check_all()`
  synchronously on the UI thread. The NVENC probe itself is a 0.1 s nullsrc
  encode (usually <2 s — the "60 s" is just its timeout), but the launch
  also runs the makemkvcon probe with a **30 s timeout** — so with an orphan
  holding the drive, the app freezes up to ~30 s before first paint. Cache
  the NVENC verdict keyed by the exact thing that can change it:
  `{ffmpeg_path, ffmpeg_version_line} → {ok, detail}` in
  `settings.extras["nvenc_probe"]` (the `extras` dict already exists; no
  schema change, and `~/.bluray-to-mp4/config.json` survives fresh zip
  extractions).
- **Why safe.** The Setup page is cosmetic for encoder choice: pipeline's
  `detect_encoder()` re-probes hardware on EVERY run start, so a stale cache
  can never send an encode down a broken path — worst case is one launch of
  slightly stale Setup text.
- **Effort.** S/M (~30 lines deps.py + a UI-side cache read; keep `deps.py`
  settings-free by passing the cached dict in and returning the stamp).
- **Risk.** Low; statefulness is the only new wrinkle. If Forge's session is
  full tonight, this one slips to v0.2.17 at zero cost.
- **Verify.** `tests/test_deps`-style unit: cache hit skips the probe
  subprocess; changed ffmpeg version line forces a re-probe. Karen: second
  launch of the app shows Setup instantly.

### QW-5 — Copy `title_overrides` into the run plan (9 characters)

- **What.** `_start_run` passes `self._title_overrides` **by reference**
  into `plan`. `run_pipeline` iterates that dict on the worker thread while
  the Review page remains reachable — if Eric edits an episode title during
  the naming pass, `episode_output_names` can hit
  `RuntimeError: dictionary changed size during iteration`. Narrow window
  (start-of-run only), real crash shape.
- **Fix.** `plan["title_overrides"] = dict(self._title_overrides)`.
- **Effort.** XS. Merge note: same line Forge extends for
  `stream_overrides` — fold in, don't race (see risk check #2).
- **Risk.** None.
- **Verify.** Existing plan tests keep passing; optionally a regression test
  asserting `plan["title_overrides"] is not window._title_overrides`.

### QW-6 — History records the number Eric actually validates: GB/hour achieved

- **What.** Pain point #2, minimal-risk slice. At `job_done` time the MP4 is
  already on disk and `job.duration_seconds` is finally populated (v0.2.13),
  so `workers.py` can record `output_bytes = os.path.getsize(...)` (guarded
  `try/except OSError`) onto `JobRecord`, and the History table/monitor can
  show size and achieved GB/hour (`bytes / duration * 3600 / 1e9`).
- **Why.** "Spot-check file size against the 2 GB/hour budget" has been on
  the pending-validation list since 2026-10-01 and is still checked by hand.
  This makes every run self-reporting — including tonight's. It also starts
  building the dataset NR-2 needs to calibrate the forecast constants
  (rip=d/40, encode=d/6 or d/1.5, OCR=d/20 are one-afternoon guesses).
- **Effort.** S (~15 lines across `monitor.py` JobRecord/history dict,
  `workers.py` job_done, `main_window.py` History column). No pipeline
  changes, no run-path changes.
- **Risk.** Low; all additions have defaults and run after the job is done.
- **Verify.** Unit: `history()` includes `output_bytes`; UI test: History
  shows "1.9 GB (2.0 GB/h)"-style text for a fake record. Karen: finish any
  job, see real numbers.

### QW-7 — DEVELOPMENT.md pending-validation rewrite (doc only)

- **What.** The "Pending validation" section still reads "paused 2026-10-01,
  no disc at hand" — ten runs ago. Proposed replacement (ready to paste) is
  in Appendix A: today's runs closed most items (real scan, BD rip→encode→
  PGS OCR chain, monitor during live runs, PRGV scaling); the genuinely open
  ones are DVD VobSub OCR end-to-end (v0.2.16 tonight), a series disc
  end-to-end, an interlaced DVD through the yadif path, an x265 real run
  (every 2026-10-02 run used NVENC), and one correction: run 7 *disproved*
  the pure sid→ordinal guarantee (the scan↔rip stream lists disagreed), so
  "stream ordinals survive the rip" should be reframed as "per-type order is
  preserved; absolute sid lookup can miss and needs the v0.2.13 fallback."
- **Effort.** XS (paste).
- **Risk.** None (docs).
- **Verify.** Eric confirms each ✓ matches his memory of the runs.

---

## NEXT RELEASE (v0.2.17 candidates, ranked)

### NR-1 — Orphan makemkvcon: auto-detect and warn at startup

- **What.** At app start (and before each drive Refresh), enumerate running
  makemkvcon processes — pure stdlib: `tasklist /FI "IMAGENAME eq
  makemkvcon.exe"` on Windows, `pgrep makemkvcon` elsewhere — and if any
  exist that aren't ours (we own ours via `_CancelState.proc`), show a
  Source/Setup banner: "A leftover MakeMKV process (PID 4812) may be holding
  the drive. Close it in Task Manager, or [End it now]." Detection is
  read-only and cross-platform-safe; the one-click kill (`taskkill /PID`)
  can be a follow-up toggle.
- **Why.** QW-3 makes the hang survivable; this makes it never mysterious.
  Orphans were the day's #1 operational failure (hits twice).
- **Effort.** S (~40 lines new `deps.py`/`binaries.py`-adjacent helper + banner
  wiring + tests with a fake tasklist output).
- **Risk.** Low. Parsing `tasklist` CSV output is dull but well-specified.
  Never kill without a visible PID and an explicit click.
- **Verify.** Unit-test the parser against recorded tasklist/pgrep output;
  Windows-side: start `makemkvcon -r info disc:9999` in another terminal,
  launch the app, see the banner.

### NR-2 — Richer, persistent history/monitor records

- **What.** Extend `JobRecord` with per-stage wall times (rip / encode / OCR
  seconds — `time.time()` deltas the pipeline already half-measures),
  `media_type`, realized encoder, output bytes (QW-6), and persist the last
  ~50 records to `~/.bluray-to-mp4/history.json` so history survives app
  restarts and version upgrades (today it is in-memory only — every fresh
  extraction Eric runs starts with an empty History page).
- **Why.** Pain point #2 properly: this is the dataset that turns the
  forecast constants from guesses into measurements, answers "NVENC vs x265
  actual speed on this box," and makes each validation run comparable to the
  last. Persistence is what makes it useful across tonight's version hops.
- **Effort.** M (~60 lines + migration-tolerant JSON load; pipeline gains
  three `t0` stamps, all off the critical path).
- **Risk.** Medium-low: touches run-path bookkeeping; stage timers must
  never raise (guard everything, the pipeline's warnings channel is the
  error surface).
- **Verify.** Fixture run with fake runners → record carries all fields;
  restart app → History repopulates from disk.

### NR-3 — Movie/series manual override on the Review page (series pre-flight)

- **What.** `detect_series` is a duration heuristic: a hybrid disc (4×22 min
  episodes + 1×90 min bonus movie) scores 90/22 ≈ 4.0 ≥ 2.2 → **classified
  movie**, every episode silently skipped. There is no user escape hatch
  today. Add a Review-page combo: `Disc type: Auto / Movie / Series`, where
  Movie/Series re-run the naming/planning pass in the forced mode. Bundle
  with the series-preflight checklist so the first series disc is boring:
  per-episode progress math (already correct — `done+frac` over plan order),
  multi-episode naming (unit-tested), queue ETA (v0.2.14/16), C6 per-episode
  failure isolation (tested).
- **Why.** Pain point #3's sharpest edge: the one failure mode that loses
  data silently, on exactly the disc type never run end-to-end.
- **Effort.** M (UI combo + plumb a `force_mode` into the naming/plan pass;
  pipeline already accepts the computed plan shape unchanged).
- **Risk.** Medium-low — pure pre-run planning change; the run path is
  untouched. Needs Eric's sign-off on UX wording.
- **Verify.** Fixture: hybrid disc forces movie → override to series →
  5 jobs appear with S01E0x names.

### NR-4 — Run the Setup dependency checks off the UI thread

- **What.** `_show_deps_if_needed` runs `check_all()` synchronously at
  startup: worst case ~30 s of frozen window (orphan makemkvcon → makemkv
  probe timeout) before the UI is usable. Move checks onto the existing
  `FetchWorker` pattern: paint the UI instantly with a "checking..." table,
  fill rows as results land. Pairs with QW-4 (cache) — cache makes
  re-checks instant, backgrounding makes first checks non-blocking.
- **Effort.** S/M (reuses FetchWorker; the table-fill logic already exists
  and just needs re-entrancy for a refresh click).
- **Risk.** Low-medium: startup sequencing changes always deserve a Karen
  pass, but no run-path impact.
- **Verify.** UI test with a slow fake check → window constructs
  immediately, rows fill on pump.

### NR-5 — Mid-encode cancel / close: orphaned ffmpeg on the hard paths

- **What.** The graceful paths are sound: cancel polls per progress line and
  `terminate()`→10 s→`kill()`s ffmpeg; closeEvent cancels, waits 30 s, then
  falls back to `QThread.terminate()`. The hole: `QThread.terminate()` kills
  the Python thread mid-read and **leaks the ffmpeg child** (orphaned,
  still burning CPU/disk). Same if the app is force-killed from Task
  Manager. Fix: keep the encode `Popen` on a cancel-handle (mirroring
  makemkv's `_CancelState.proc`) so `RunWorker`/closeEvent can always kill
  ffmpeg before the thread-level last resort.
- **Why.** Pain point #6. Today's answer is "graceful cancel is safe; the
  only leak vector is the last-resort/force-kill path." Worth closing before
  series discs make 4-hour queues and impatient closes more likely.
- **Effort.** S/M (~30 lines; one handle, two kill sites).
- **Risk.** Medium — it touches cancel/cleanup; needs the existing cancel
  regression tests plus one new "worker terminated with live fake ffmpeg →
  kill called" test.
- **Verify.** Tests above + manual: start encode, close app, answer Yes,
  confirm no ffmpeg in Task Manager.

### NR-6 — Overwrite guard when target MP4 already exists

- **What.** `build_encode_args` passes `-y`: a re-run silently overwrites
  yesterday's finished MP4. During validation that's desired; for a novice
  it's a foot-gun (typo'd series name → re-rip nukes a good encode hours
  later). At Start, if any planned output exists, one dialog:
  "3 of these files already exist — [Overwrite all] [Rename new files]
  [Cancel]."
- **Effort.** S (existence check over the plan list + QMessageBox; no
  pipeline change).
- **Risk.** Low, but deliberately NOT tonight (see risk check).
- **Verify.** UI test: pre-create a target file → dialog appears; overwrite
  path keeps `-y`.

### NR-7 — Series bookkeeping + small settings persistence

- **What.** After a series run, remember per-show progress
  (`settings.extras["series_progress"][show] = last_episode_done`) and
  pre-fill season/start-episode on the next scan of a similar disc name;
  a one-line hint after the run: "Next disc of this show starts at S01E09."
  Also persist the "Include subtitles" checkbox (currently resets to on each
  launch — a fine default, but a toggled-off choice should stick).
- **Why.** Multi-disc seasons are the next untested workflow; Eric currently
  does the episode math by hand between discs.
- **Effort.** S.
- **Risk.** Low; prefill is a suggestion, never a block.
- **Verify.** Unit on the prefill logic; UI test that a second series scan
  inherits the episode counter.

---

## NOT YET

- **NY-1 — Windows Job Objects (kill-on-close) for crash-proof child
  reaping.** The *right* end-state for orphan-proofing ffmpeg/makemkvcon
  even when the app is force-killed — but it's ctypes + Windows-only, and
  this box can't test it. Do NR-5 first; revisit only if leaks still occur.
- **NY-2 — `CREATE_NO_WINDOW` on subprocess spawns (console-flash
  prevention).** Speculative: no validation report mentioned console
  flashes. Ask Eric whether he ever saw a black window flicker; if not,
  drop the idea.
- **NY-3 — Auto-eject the disc on completion.** Charming novice touch
  ("computer gives you the disc back when it's done"), but it needs
  Windows-only ctypes (`mciSendString`), can't be tested here, and tonight's
  queue stability outranks it.
- **NY-4 — "Skip this episode, keep the queue" mid-run.** EncodeCancelled
  currently ends the whole run, which is correct for movies. Per-episode
  skip adds cancel-semantics churn right as series support lands; wait for
  the first real series disc to say whether it's needed.
- **NY-5 — Auto-calibrating forecasts.** Fitting the rip/encode/OCR
  constants to recorded runs is exactly right — after NR-2 has collected
  the data. Doing it now means curve-fitting two samples.
- **NY-6 — Tray minimize / completion notification.** Pleasant, solves a
  problem Eric hasn't reported; keep the queue rock-solid first.
- **NY-7 — Refactoring `main_window.py` (1,270 lines).** It earns a split
  eventually, but any restructure during a validation-window release cadence
  is risk without user-visible payoff. Revisit when the version curve
  flattens.

---

## Pain-point coverage matrix

| # | Eric's pain point | Answered by |
|---|---|---|
| 1 | Orphan makemkvcon auto-detect | QW-3 (timeout makes guidance reachable tonight) → NR-1 (proactive banner + optional kill) |
| 2 | Thin monitor/history data | QW-6 (size + achieved GB/h tonight) → NR-2 (per-stage times, media type, persistence) |
| 3 | Series pre-flight | NR-3 (movie/series override — the silent-data-loss fix), NR-7 (bookkeeping); per-episode progress/ETA/naming already verified in tests |
| 4 | Pending-validation list stale | QW-7 (ready-to-paste rewrite, Appendix A) |
| 5 | NVENC probe each launch | QW-4 (version-stamped cache; run-time re-probe keeps it safe) → NR-4 (background checks kill the 30 s launch freeze) |
| 6 | Cancel mid-encode / orphan ffmpeg | Analysis: graceful path sound; leak only via force-kill/last-resort → NR-5 (Job Objects deferred as NY-1) |
| 7 | Fresh extractions lose settings? | **No — verified non-issue.** `config.py` writes `~/.bluray-to-mp4/config.json` in the user profile, outside the app folder. Outdir, TMDB key, monitor toggle, encoder, theme all survive a fresh zip. Nothing to fix; one gap — season/start-episode and the subs checkbox don't persist — folded into NR-7. |

---

## Appendix A — proposed DEVELOPMENT.md "Pending validation" replacement

```markdown
## Pending validation

Validated by the 2026-10-02 runs (v0.2.6–v0.2.14, ten real discs/runs):

- ✓ Real-disc scan → rip → ffprobe → encode chain (Blu-ray; runs 3–10)
- ✓ DVD encode path at the SD budget (first DVD run, 2026-10-02)
- ✓ PGS subtitle extraction → OCR → sidecar SRT (Blu-ray)
- ✓ Monitor API watched during live runs (runs 4, 7)
- ✓ PRGV scaling and rip ETAs (post-v0.2.9 flag fix)
- ✓ Stream-order preservation (per type) between scan and rip —
  with one correction: run 7 proved the absolute sid→ordinal lookup can
  MISS (scan and rip stream lists disagreed). The v0.2.13 first-subtitle
  fallback is now a permanent part of the contract, not an edge case.

Still open:

- DVD VobSub OCR end-to-end (fix committed in v0.2.16; validate tonight)
- Embedded mov_text subtitles inside the MP4 (v0.2.16; validate tonight)
- Series disc end-to-end (multi-episode naming, queue math, per-episode
  OCR) — never run on real media
- Interlaced DVD through the yadif path (`is_interlaced` has no confirmed
  real-media trigger yet)
- x265 software encoder on a real run (every 2026-10-02 run used NVENC)
- File size vs the 2 GB/hour (BD) / 1.0 GB/hour (DVD, v0.2.16) budgets —
  now self-reported in History (QW-6); spot-check against real outputs
- Subtitle sync/accuracy spot-check on a finished MP4 (manual, Eric)
```
