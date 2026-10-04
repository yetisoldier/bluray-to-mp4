"""Subtitle extraction and OCR: PGS/VOBsub from ripped MKV to sidecar SRT.

Blu-ray subtitles are image-based (PGS) and MP4 cannot contain them, so
the recommended track is extracted to .sup and OCR'd with Subtitle
Edit's headless command-line converter, seconv (Tesseract engine). DVDs
carry VobSub, which extracts to a single-track MKV instead (v0.2.16:
ffmpeg has no vobsub muxer; matroska is the only container that
accepts a dvd_subtitle stream copy).

v0.2.27 makes the stage fully unattended (Eric's 2026-10-02 zero-
interaction criterion, unmet through run 12 where the bundled SE 5.2.0
GUI - which has no /convert mode - opened the interactive OCR wizard):
seconv OCRs both PGS and VobSub with no Start button and no glyph
dialogs, a second seconv pass auto-resolves unknown glyphs with best
guesses via the bundled OCR-fix dictionary, and the run records the
auto-resolved cue count as an honest warning instead of passing
silently. The SRT output is still parsed and normalized locally; a
zero-cue result is treated as an OCR failure and never silently
shipped. The normalized SRT is kept as an editable sidecar next to the
MP4 and embedded into the MP4 as a soft mov_text track (v0.2.16).
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

from .binaries import resolve_ffmpeg, resolve_subtitle_edit
from .srt import Cue, SrtError, normalize_cues, parse_srt, write_srt_file

__all__ = [
    "SubtitleOcrError",
    "OcrResult",
    "OcrRunOutcome",
    "SubtitleEditOcr",
    "SE_CONV_SETTINGS_NAME",
    "extract_subtitles",
    "run_subtitle_ocr",
    "write_ocr_settings",
    "mux_language",
    "mux_srt_into_mp4",
]

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


class SubtitleOcrError(RuntimeError):
    """Subtitle extraction or OCR failed."""


# Settings seed handed to every seconv invocation via /settings:.
SE_CONV_SETTINGS_NAME = "b2m-seconv-settings.json"

# The SE 5.x GUI cannot run unattended OCR (no /convert mode) - invoking
# it is exactly the run-12 wizard interaction, so the wrapper refuses it.
_GUI_BINARY_NAMES = {"subtitleedit.exe", "subtitleedit"}

# ISO 639-2 (what Tesseract wants, e.g. "eng") -> ISO 639-1 for seconv's
# OCR-fix language gate (e.g. "en").
_FCE_LANGUAGES = {
    "eng": "en", "deu": "de", "fra": "fr", "spa": "es", "ita": "it",
    "nld": "nl", "por": "pt", "swe": "sv", "dan": "da", "nor": "no",
    "fin": "fi", "pol": "pl",
}


@dataclass
class OcrResult:
    srt_path: str
    cue_count: int
    notes: list[str] = field(default_factory=list)


def extract_subtitles(
    mkv_path: str,
    stream_index: int,
    out_path: str,
    *,
    binary: str = "ffmpeg",
) -> str:
    """Extract one subtitle stream (PGS/VOBsub) losslessly to its own file."""
    binary = resolve_ffmpeg(binary)
    try:
        result = subprocess.run(
            [
                binary, "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
                "-i", mkv_path, "-map", f"0:{stream_index}", "-c", "copy", out_path,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise SubtitleOcrError(
            f"ffmpeg not found for extraction: {exc}. Install ffmpeg from "
            "https://ffmpeg.org/download.html and retry."
        ) from exc
    if result.returncode != 0 or not os.path.exists(out_path):
        raise SubtitleOcrError(
            f"subtitle extraction failed: {result.stderr.strip()[:300]}"
        )
    return out_path


def write_ocr_settings(path: str) -> None:
    """Seed seconv's settings JSON before OCR runs (v0.2.27).

    seconv starts from libse's built-in defaults - never from a user's
    GUI config - so this seed is the explicit, version-pinned statement
    of the OCR text contract (conservative libse line/duration limits)
    that every run hands to the engine via /settings:. Every key is a
    GeneralSection member of seconv 5.2.0's settings schema, so the
    invocation never triggers seconv's unknown-key warning.
    """
    seed = {
        "general": {
            "currentFrameRate": 23.976,
            "defaultFrameRate": 23.976,
            "subtitleLineMaximumLength": 43,
            "maxNumberOfLines": 2,
            "mergeLinesShorterThan": 33,
            "minimumMillisecondsBetweenLines": 24,
            "subtitleMinimumDisplayMilliseconds": 1000,
            "subtitleMaximumDisplayMilliseconds": 8000,
            "subtitleMaximumCharactersPerSeconds": 25.0,
            "subtitleOptimalCharactersPerSeconds": 15.0,
        }
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(seed, fh, indent=2)
        fh.write("\n")


def _tesseract_exe_name() -> str:
    return "tesseract.exe" if os.name == "nt" else "tesseract"


def _tesseract_search_dirs(se_binary: str) -> list[str]:
    """Directories that may hold a tesseract binary for seconv's engine.

    seconv's Tesseract engine scans the PATH only, so the wrapper seeds
    the subprocess PATH with every place a working tesseract lives:
    the app's bundled copy (tools/Tesseract beside the bundled
    tools/SubtitleEdit), the SE GUI's own downloaded copy in the user
    data folder, and the standard UB Mannheim install dirs - in that
    priority order. Returns only dirs that actually contain the binary.
    """
    exe = _tesseract_exe_name()
    candidates: list[str] = []
    try:
        se_dir = os.path.dirname(os.path.abspath(se_binary))
    except OSError:
        se_dir = ""
    for base in (se_dir, os.path.dirname(se_dir) if se_dir else ""):
        if base:
            candidates.append(os.path.join(base, "Tesseract"))
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        candidates.append(os.path.join(appdata, "Subtitle Edit", "Tesseract"))
    for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(env_name, "")
        if root:
            candidates.append(os.path.join(root, "Tesseract-OCR"))
    return [
        cand for cand in candidates
        if os.path.isfile(os.path.join(cand, exe))
    ]


def _ocr_env(se_binary: str) -> dict:
    """Environment for the seconv subprocess: PATH seeded with known
    Tesseract locations (the bundled copy first) and TESSDATA_PREFIX
    pointing at the bundled tessdata when present, so a fresh zip
    extraction can OCR with no Tesseract install at all. Unchanged when
    nothing is found - a system-PATH tesseract then serves as before."""
    env = os.environ.copy()
    dirs = _tesseract_search_dirs(se_binary)
    if dirs:
        env["PATH"] = os.pathsep.join(dirs + [env.get("PATH", "")])
        tessdata = os.path.join(dirs[0], "tessdata")
        if os.path.isfile(os.path.join(tessdata, "eng.traineddata")):
            env["TESSDATA_PREFIX"] = tessdata
    return env


def _count_auto_resolved_cues(raw_text: str, fixed_text: str) -> int:
    """Number of cues whose text the auto-fix pass changed (unknown
    glyphs that were best-guessed). Defensive by design: the count is
    honest reporting, never a gate - any parse problem yields 0 rather
    than failing an otherwise-good OCR result."""
    try:
        raw = normalize_cues(parse_srt(raw_text))
        fixed = normalize_cues(parse_srt(fixed_text))
    except SrtError:
        return 0
    return sum(1 for a, b in zip(raw, fixed) if a.text != b.text)


@dataclass
class OcrRunOutcome:
    """Result of the two unattended seconv passes."""

    srt_path: str        # final SRT in out_dir (what ships downstream)
    raw_srt_path: str    # pass-1 raw OCR output (== srt_path when the fix pass failed)
    fix_applied: bool    # the pass-2 auto-fix ran successfully
    fix_error: str = ""  # why the auto-fix pass failed, when fix_applied is False


@dataclass
class SubtitleEditOcr:
    """OCR via Subtitle Edit's headless seconv converter (Tesseract).

    v0.2.27: the run is a two-pass, fully unattended flow (Eric's
    2026-10-02 criterion - no Start button, no glyph dialogs):
      pass 1 - raw OCR of every image (Tesseract best guess per glyph),
      pass 2 - a seconv OCR-fix pass (FixCommonOcrErrors with the
               English replace list + Hunspell guessing, both bundled
               inside seconv) that auto-resolves unknown glyphs.
    Neither pass ever interacts with a human; the caller reports the
    honest auto-resolved count. The SE 5.x GUI binary is refused
    outright - it has no unattended mode at all (run 12's wizard).
    """

    binary: str = "seconv"
    ocr_engine: str = "tesseract"
    language: str = "eng"   # ISO 639-2: what Tesseract itself wants

    def __post_init__(self) -> None:
        self._refuse_gui_binary(self.binary)

    @staticmethod
    def _refuse_gui_binary(binary: str) -> None:
        """Never drive the SE 5.x GUI: it has no unattended mode at all
        (run 12's wizard - Start button + glyph dialogs), so refuse it
        both at construction and after binary resolution, before any
        subprocess spawn. Backslashes are normalized so a Windows path
        is recognized on every platform."""
        base = os.path.basename((binary or "").replace("\\", "/")).lower()
        if base in _GUI_BINARY_NAMES:
            raise SubtitleOcrError(
                "Subtitle Edit's GUI cannot run unattended OCR (its 5.x "
                "/convert mode is gone); seconv.exe - the headless "
                "converter - is required. Reinstall the latest release "
                "zip to get the bundled seconv."
            )

    def command(self, in_path: str, out_dir: str) -> list[str]:
        """seconv CLI: /convert <pattern> <format> /ocrengine:<engine>.

        The pass-1 (raw OCR) invocation. /outputfilename pins the exact
        output name: seconv's derived name for MKV image tracks carries
        a language suffix (movie.und.srt), which is not the name the
        sidecar contract expects. Legacy slash syntax is fully
        supported by seconv 5.2.0 (it drops /convert and rewrites
        /option:value to --option:value internally).
        """
        return [
            self.binary,
            "/convert", in_path,
            "subrip",
            f"/ocrengine:{self.ocr_engine}",
            f"/ocrlanguage:{self.language}",
            "/settings:" + os.path.join(out_dir, SE_CONV_SETTINGS_NAME),
            "/outputfilename:b2m_ocr_raw.srt",
            "/outputfolder:" + out_dir,
            "/overwrite",
        ]

    def fix_command(self, raw_srt_path: str, out_dir: str) -> list[str]:
        """Pass 2: auto-resolve unknown glyphs (OCR-fix pass, headless).

        FixCommonOcrErrors is seconv's headless equivalent of the GUI's
        unknown-glyph best-guess step: it applies the English OCR
        replace list and Hunspell-driven guessing using dictionaries
        bundled inside seconv itself, so it needs no user training and
        never prompts. /fce-language pins the gate to the OCR language
        (en) so the pass runs even on sparse output.
        """
        return [
            self.binary,
            "/convert", raw_srt_path,
            "subrip",
            "/fixcommonerrorsrules:FixCommonOcrErrors",
            f"/fce-language:{_FCE_LANGUAGES.get(self.language, 'en')}",
            "/settings:" + os.path.join(out_dir, SE_CONV_SETTINGS_NAME),
            "/outputfilename:b2m_ocr_fixed.srt",
            "/outputfolder:" + out_dir,
            "/overwrite",
        ]

    def _run_one(
        self, cmd: list[str], *, runner: Runner | None
    ) -> "subprocess.CompletedProcess[str]":
        """Execute one seconv pass (shared error surface)."""
        if runner is None:
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                    env=_ocr_env(self.binary),
                )
            except FileNotFoundError as exc:
                raise SubtitleOcrError(
                    "Subtitle Edit seconv not found on PATH. Install Subtitle Edit from "
                    "https://github.com/SubtitleEdit/subtitleedit/releases "
                    "with seconv.exe (and Tesseract via the UB Mannheim build), then retry. "
                    f"Details: {exc}"
                ) from exc
            if proc.returncode != 0:
                raise SubtitleOcrError(
                    f"Subtitle Edit seconv exited with code {proc.returncode}: "
                    f"{(proc.stderr or proc.stdout).strip()[:300]}"
                )
            return proc
        proc = runner(cmd)
        if proc.returncode != 0:
            raise SubtitleOcrError(
                f"Subtitle Edit seconv exited with code {proc.returncode}"
            )
        return proc

    def run(
        self,
        in_path: str,
        out_dir: str,
        *,
        runner: Runner | None = None,
    ) -> OcrRunOutcome:
        """Run both unattended passes and return the outcome.

        Pass 1 (raw OCR) failing raises SubtitleOcrError - the existing
        fail-open contract (no SRT is produced, nothing ships). Pass 2
        (auto-fix) failing is NOT a hard failure: the raw OCR is still
        usable, so the raw SRT ships with fix_applied=False and the
        reason - the caller turns that into an honest warning.
        """
        cmd = self.command(in_path, out_dir)
        self.binary = resolve_subtitle_edit(self.binary)
        self._refuse_gui_binary(self.binary)  # a GUI-only install must never spawn the wizard
        cmd[0] = self.binary
        # Self-contained seeding: run_subtitle_ocr writes the seed up
        # front (its contract test pins that), but a direct run() caller
        # must not die on a missing settings file either.
        settings_path = os.path.join(out_dir, SE_CONV_SETTINGS_NAME)
        if not os.path.exists(settings_path):
            write_ocr_settings(settings_path)
        # v0.2.29 (Karen's validation): purge a stale raw SRT BEFORE
        # pass 1 - a killed prior OCR in this workdir leaves one
        # behind, and a seconv pass-1 that exits 0 without writing
        # would otherwise ship that stale file (a different title's
        # subtitles) under a success path. Symmetric with the
        # fixed_srt cleanup before pass 2 below.
        raw_srt = os.path.join(out_dir, "b2m_ocr_raw.srt")
        if os.path.exists(raw_srt):
            try:
                os.remove(raw_srt)  # stale from a killed run
            except OSError:
                pass
        self._run_one(cmd, runner=runner)

        if not os.path.exists(raw_srt):
            raise SubtitleOcrError(
                f"Subtitle Edit seconv produced no SRT (expected {raw_srt}). "
                "Check the Tesseract installation."
            )

        fixed_srt = os.path.join(out_dir, "b2m_ocr_fixed.srt")
        if os.path.exists(fixed_srt):
            try:
                os.remove(fixed_srt)  # stale from a killed run
            except OSError:
                pass
        fix_cmd = self.fix_command(raw_srt, out_dir)
        fix_cmd[0] = self.binary
        try:
            self._run_one(fix_cmd, runner=runner)
        except SubtitleOcrError as exc:
            if os.path.exists(fixed_srt):
                # It exited nonzero but still wrote the file - usable.
                return OcrRunOutcome(fixed_srt, raw_srt, True)
            return OcrRunOutcome(raw_srt, raw_srt, False, str(exc))
        if not os.path.exists(fixed_srt):
            return OcrRunOutcome(
                raw_srt, raw_srt, False,
                "auto-fix pass produced no SRT",
            )
        return OcrRunOutcome(fixed_srt, raw_srt, True)


def _intermediate_ext(codec_name: str) -> str:
    """Container extension for the extracted subtitle track.

    PGS (hdmv_pgs_subtitle) -> .sup, DVD VobSub (dvd_subtitle) -> .mkv
    (ffmpeg's only muxer that accepts dvd_subtitle copy; Subtitle Edit
    reads VobSub from MKV directly). Unknown subtitle codecs also get
    .mkv: MKV is the most permissive container, so it fails last, and
    the SubtitleEditOcr error names the input file for diagnosis.
    """
    return ".sup" if codec_name == "hdmv_pgs_subtitle" else ".mkv"


def run_subtitle_ocr(
    mkv_path: str,
    stream_index: int,
    video_out_path: str,
    *,
    workdir: str,
    codec_name: str = "",
    se: SubtitleEditOcr | None = None,
    extract_binary: str = "ffmpeg",
    se_runner: Runner | None = None,
) -> OcrResult:
    """Full path: extract -> OCR -> normalize -> <video>.srt sidecar.

    codec_name (v0.2.16) is the probed subtitle codec: PGS
    (hdmv_pgs_subtitle; the "" default keeps v0.2.15 callers and tests
    source-compatible) extracts to .sup; DVD VobSub (dvd_subtitle) and
    unknown codecs extract to a single-track .mkv intermediate - the
    only ffmpeg muxer that accepts a dvd_subtitle copy, which SE's
    OCR reads directly (no mkvextract, no new dependency).
    """
    se = se or SubtitleEditOcr()
    os.makedirs(workdir, exist_ok=True)
    if not codec_name:
        # "" is the source-compatible default (v0.2.15 callers/tests):
        # behave as PGS. _intermediate_ext("") itself still maps to .mkv
        # - that is the separate "unknown codec" contract.
        codec_name = "hdmv_pgs_subtitle"

    img_path = (
        os.path.splitext(os.path.basename(mkv_path))[0]
        + _intermediate_ext(codec_name)
    )
    img_path = os.path.join(workdir, img_path)
    # v0.2.16 guard (deviation from the design doc, found by testing
    # real ffmpeg): the pipeline's workdir IS the ripped MKV's own
    # directory, so the VobSub branch derives the intermediate as the
    # input file itself (same stem, same .mkv extension) - ffmpeg then
    # refuses with "Output ... same as Input #0 - exiting" and every
    # real DVD rip would fail extraction. In that one case the
    # intermediate gets a distinguishing suffix; PGS (.sup) never
    # collides, and a non-colliding workdir keeps the design's plain
    # <stem>.mkv name.
    if os.path.normcase(os.path.abspath(img_path)) == os.path.normcase(
        os.path.abspath(mkv_path)
    ):
        img_path = os.path.join(
            workdir,
            os.path.splitext(os.path.basename(mkv_path))[0] + ".vobsub.mkv",
        )
    extract_subtitles(mkv_path, stream_index, img_path, binary=extract_binary)

    # v0.2.27: seed seconv's settings before OCR runs (the explicit,
    # version-pinned OCR text contract; seconv otherwise uses libse
    # defaults - never a user's GUI config), then run both unattended
    # passes and report the auto-resolved glyph count honestly.
    settings_path = os.path.join(workdir, SE_CONV_SETTINGS_NAME)
    write_ocr_settings(settings_path)
    outcome = se.run(img_path, workdir, runner=se_runner)
    with open(outcome.raw_srt_path, encoding="utf-8-sig") as fh:
        raw_text = fh.read()
    with open(outcome.srt_path, encoding="utf-8-sig") as fh:
        raw = fh.read()

    fixed_parse_failed = False
    try:
        cues = parse_srt(raw)
    except SrtError as exc:
        if outcome.srt_path == outcome.raw_srt_path:
            # The shipping SRT IS the raw OCR - there is no distinct
            # fixed file to fall back from, so the fail-closed
            # contract stands.
            raise SubtitleOcrError(f"OCR output was not valid SRT: {exc}") from exc
        # v0.2.29 (Karen's validation): pass 2 exited 0 but its file
        # does not parse - a truncated partial write. The raw OCR is
        # still good, so fail open to it instead of raising and losing
        # the subtitles to broken post-processing.
        try:
            cues = parse_srt(raw_text)
        except SrtError:
            raise SubtitleOcrError(f"OCR output was not valid SRT: {exc}") from exc
        raw = raw_text
        fixed_parse_failed = True
    cues = normalize_cues(cues)
    notes: list[str] = []
    if not cues:
        raise SubtitleOcrError(
            "OCR completed but produced zero cues; the subtitle track is "
            "likely unusable (or Tesseract needs its English data)."
        )
    # The honest unattended-OCR report: never a silent pass. The
    # auto-fix pass's changed-cue count is ALWAYS named when it ran -
    # including 0, which proves the pass executed - and unknown glyphs
    # best-guessed above zero get the spot-check wording (the run-12
    # defect was the silence, not the guessing). A fixed file that
    # failed to parse is named too: the raw OCR shipped instead.
    if fixed_parse_failed:
        notes.append(
            "Unattended OCR auto-fix output was not valid SRT (likely a "
            "truncated write); the raw OCR shipped without glyph "
            "corrections - spot-check spelling."
        )
    elif outcome.fix_applied:
        auto_resolved = _count_auto_resolved_cues(raw_text, raw)
        notes.append(
            f"Unattended OCR auto-resolved {auto_resolved} of "
            f"{len(cues)} cues with best-guess replacements"
            + ("; spot-check those for glyph errors."
               if auto_resolved else ".")
        )
    else:
        notes.append(
            "Unattended OCR auto-fix pass failed ("
            f"{outcome.fix_error[:200]}); the raw OCR shipped without "
            "glyph corrections - spot-check spelling."
        )
    if len(cues) < 50:
        notes.append(
            f"Only {len(cues)} cues detected; check a few against the film."
        )
    notes.append("Tesseract OCR is ~95-99% accurate; spot-check spelling.")

    sidecar = os.path.splitext(video_out_path)[0] + ".srt"
    write_srt_file(sidecar, cues)
    os.remove(img_path)
    for tmp in (outcome.raw_srt_path, outcome.srt_path, settings_path):
        if tmp != sidecar:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return OcrResult(srt_path=sidecar, cue_count=len(cues), notes=notes)


def mux_language(lang: str) -> str:
    """ffprobe subtitle language -> MP4 track tag.

    'eng'/'en' -> 'eng'; anything else (incl. '', the v0.2.14
    unconfirmed case) -> 'und'. The OCR pipeline hard-codes
    /ocrlanguage:eng, so stamping 'eng' on an unconfirmed stream would
    make the honest case indistinguishable from the confirmed one in
    players' subtitle menus; 'und' is the container's defined
    "undetermined" value and every player renders it fine.
    """
    return "eng" if lang in ("eng", "en") else "und"


def mux_srt_into_mp4(
    video_path: str,
    srt_path: str,
    *,
    language: str = "und",
    binary: str = "ffmpeg",
    runner: Runner | None = None,
) -> str:
    """Embed srt_path as a soft mov_text subtitle track in video_path.

    Muxes to a temp sibling then os.replace()s it over video_path, so a
    failed or interrupted mux can never destroy the finished encode: on
    any failure the temp is deleted and video_path stays bit-identical
    (v0.2.16). mov_text is the only subtitle codec the MP4 container
    supports and the one every player (VLC, Plex, Jellyfin, Apple TV)
    reads from .mp4.
    """
    binary = resolve_ffmpeg(binary)
    tmp = video_path + ".muxing.mp4"  # sibling: same fs -> atomic replace
    if os.path.exists(tmp):
        try:
            os.remove(tmp)  # stale temp from a killed run
        except OSError:
            pass
    cmd = [
        binary, "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
        "-i", video_path, "-i", srt_path,
        "-map", "0", "-map", "1",
        "-c", "copy", "-c:s", "mov_text",
        "-metadata:s:s:0", f"language={mux_language(language)}",
        tmp,
    ]
    if runner is None:
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
        except FileNotFoundError as exc:
            raise SubtitleOcrError(
                f"ffmpeg not found for subtitle muxing: {exc}. Install "
                "ffmpeg from https://ffmpeg.org/download.html and retry."
            ) from exc
    else:
        proc = runner(cmd)
    if proc.returncode != 0 or not os.path.exists(tmp) or (
        os.path.getsize(tmp) == 0
    ):
        try:
            os.remove(tmp)
        except OSError:
            pass
        stderr = (getattr(proc, "stderr", "") or "").strip()
        raise SubtitleOcrError(f"subtitle muxing failed: {stderr[:300]}")
    try:
        os.replace(tmp, video_path)
    except OSError as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise SubtitleOcrError(
            f"subtitle muxing could not replace the MP4: {exc}"
        ) from exc
    return video_path