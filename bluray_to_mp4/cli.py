"""Command-line interface: drives | scan | run | probe-stream."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field

from . import __version__
from .makemkv import MakeMkvError, find_drives, scan_disc
from .recommend import recommend_title
from .series import detect_series, main_feature

__all__ = ["main", "build_parser"]


def _fmt_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _excerpt(text: str, limit: int = 200) -> str:
    """One Telegram-friendly line from multi-line probe output."""
    text = (text or "").strip().replace("\r", "")
    joined = " | ".join(l.strip() for l in text.splitlines() if l.strip())
    return joined[:limit] + "..." if len(joined) > limit else joined


# ffmpeg stderr: "Stream #0:2[0x...]: Subtitle: pgs (...)" -> (subtitle, pgs)
_FF_STREAM_RE = re.compile(r"Stream #0:\d+.*?: (Video|Audio|Subtitle): ([\w.-]+)")


@dataclass
class _UrlProbe:
    """Result of probing one stream-server title URL."""

    ok: bool                       # URL was opened and read
    prober: str                    # 'ffprobe' | 'ffmpeg null read' | 'failed'
    format_name: str               # container; '' when unknown
    duration: float | None         # container duration; None = not reported
    streams: list[tuple[str, str]] = field(default_factory=list)  # (type, codec)
    stderr_excerpt: str = ""      # prober error context


def _run_ffprobe_json(url: str, timeout: float, binary: str = "ffprobe"):
    """ffprobe -show_streams -show_format on a URL; (rc, stdout, stderr)."""
    from .binaries import resolve_ffprobe

    binary = resolve_ffprobe(binary)
    result = subprocess.run(
        [binary, "-v", "error", "-show_streams", "-show_format",
         "-of", "json", url],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout,
    )
    return result.returncode, result.stdout, result.stderr


def _run_ffmpeg_read(url: str, timeout: float, binary: str = "ffmpeg"):
    """Short ffmpeg null read of a URL (5 s of stream); (rc, stderr)."""
    from .binaries import resolve_ffmpeg

    binary = resolve_ffmpeg(binary)
    result = subprocess.run(
        [binary, "-hide_banner", "-nostdin", "-v", "info",
         "-i", url, "-t", "5", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout,
    )
    return result.returncode, result.stderr


def _parse_ffprobe_json(text: str) -> tuple[str, float | None, list[tuple[str, str]]]:
    """(format_name, duration, streams) from ffprobe JSON output."""
    data = json.loads(text)
    fmt = data.get("format") or {}
    duration = None
    try:
        if fmt.get("duration"):
            duration = float(fmt["duration"])
    except (TypeError, ValueError):
        duration = None
    streams = [
        (s.get("codec_type", ""), s.get("codec_name", ""))
        for s in data.get("streams", [])
    ]
    return fmt.get("format_name", "") or "", duration, streams


def _parse_ffmpeg_streams(stderr: str) -> list[tuple[str, str]]:
    """Stream inventory from ffmpeg 'Stream #0:N: Type: codec' stderr lines."""
    return [(m.group(1).lower(), m.group(2)) for m in _FF_STREAM_RE.finditer(stderr)]


def _probe_url(url: str, timeout: float,
               ffprobe_bin: str = "ffprobe",
               ffmpeg_bin: str = "ffmpeg") -> _UrlProbe:
    """Probe one title URL: ffprobe first, short ffmpeg read as fallback.

    Returns a _UrlProbe in every case; ok=False carries the combined
    failure excerpts for the report (never raises for probe failures).
    """
    failures: list[str] = []
    try:
        rc, out, err = _run_ffprobe_json(url, timeout, ffprobe_bin)
        if rc == 0:
            try:
                fmt, dur, streams = _parse_ffprobe_json(out)
                return _UrlProbe(True, "ffprobe", fmt, dur, streams)
            except ValueError:  # unparsable JSON: unusable output
                failures.append("ffprobe: unparsable output")
        failures.append(f"ffprobe: {_excerpt(err) or f'exit code {rc}'}")
    except subprocess.TimeoutExpired:
        failures.append(f"ffprobe: timed out after {timeout:.0f}s")
    except OSError:
        failures.append("ffprobe: not runnable")
    try:
        rc, err = _run_ffmpeg_read(url, timeout, ffmpeg_bin)
        if rc == 0:
            return _UrlProbe(True, "ffmpeg null read", "", None,
                             _parse_ffmpeg_streams(err))
        failures.append(f"ffmpeg: {_excerpt(err) or f'exit code {rc}'}")
    except subprocess.TimeoutExpired:
        failures.append(f"ffmpeg: timed out after {timeout:.0f}s")
    except OSError:
        failures.append("ffmpeg: not runnable")
    return _UrlProbe(False, "failed", "", None, [], "; ".join(failures))


def cmd_drives(args) -> int:
    drives = find_drives()
    if not drives:
        print("No optical drives found.")
        return 1
    for d in drives:
        state = {
            0: "empty",
            1: "open",
            2: "ready",
            3: "loading",
        }.get(d.status, f"status {d.status}")
        disc = f" [{d.disc_type.upper()}: {d.disc_label}]" if d.has_disc else ""
        print(f"disc:{d.index}  {state:<8}  {d.drive_name}{disc}")
    return 0


def cmd_scan(args) -> int:
    disc = scan_disc(args.index)
    guess = detect_series(disc)
    kind = "SERIES disc" if guess.is_series else "MOVIE disc"
    print(f"Disc {args.index}: {disc.name} ({disc.media_type or 'unknown'} type)")
    print(f"Detected: {kind}")
    for reason in guess.reasons:
        print(f"  - {reason}")
    print()
    for t in disc.titles:
        sel = recommend_title(t)
        audios = t.streams_of("audio")
        subs = t.streams_of("subtitle")
        best_audio = next(
            (a for a in audios if a.sid == sel.audio_sid), None
        )
        audio_desc = ""
        if best_audio is not None:
            audio_desc = (
                f"recommended audio: {best_audio.lang_name or '?'} "
                f"{best_audio.codec_long} ({best_audio.channels}ch)"
            )
        print(
            f"Title {t.tid}: {_fmt_duration(t.duration_seconds)}  "
            f"{t.chapters} ch  {len(audios)} audio / {len(subs)} subs  {audio_desc}"
        )
        for w in sel.warnings:
            print(f"    ! {w}")
    return 0


def cmd_run(args) -> int:
    from .pipeline import run_pipeline

    def stage(msg: str):
        print(msg, flush=True)

    def progress(update):
        kind, tid, frac = update[:3]  # encode progress may append ETA (4th)
        eta = update[3] if len(update) > 3 else None
        bar = "#" * int(frac * 40)
        tail = f" eta {int(eta)//60}:{int(eta)%60:02d}" if eta is not None else ""
        print(
            f"\r  title {tid} {kind}: [{bar:<40}] {frac * 100:5.1f}%{tail}",
            end="",
            flush=True,
        )
        if frac >= 1.0:
            print()

    result = run_pipeline(
        args.index,
        args.out,
        encoder=args.encoder,
        dry_run=args.dry_run,
        series_name=args.series_name,
        season=args.season,
        start_episode=args.start_episode,
        episode_titles=None,
        year=args.year,
        tmdb_key=args.tmdb_key,
        on_stage=stage,
        on_progress=progress if not args.dry_run else None,
    )

    print()
    print(f"Disc: {result.disc_name} ({result.media_type})")
    print(f"Mode: {'series' if result.is_series else 'movie'}  Encoder: {result.encoder}")
    for reason in result.reasons:
        print(f"  - {reason}")
    for job in result.jobs:
        status = "would create" if args.dry_run else ("done" if job.completed else "pending")
        print(f"  {status:<12} {job.output_name}")
    for w in result.warnings:
        print(f"  ! {w}")
    return 0


# A diagnostic must identify itself (Eric, 2026-10-02 probe.3 rule):
# two RED runs were byte-identical and we could not tell which build
# produced them. The probe prints its build stamp as the first line.
PROBE_ID = "probe.4"


def _print_stream_attempts(attempts) -> None:
    """probe.4: the argument-variant matrix, one block per dead variant.

    probe.3's report had two blind spots: the ~300-char stdout excerpt
    let the real error line scroll past, and stderr was discarded. Each
    StreamAttempt carries its argv shape, exit code, and ~400-char
    stdout ('out:') and stderr ('err:') tails - printed here BEFORE the
    verdict so the report shows exactly which shape makemkvcon accepts.
    """
    for att in attempts:
        print(f"Attempt {att.name}: {' '.join(att.args)} "
              f"-> exit {att.exit_code}")
        print(f"  out: {_excerpt(att.stdout_tail, 400) or '(none)'}")
        print(f"  err: {_excerpt(att.stderr_tail, 400) or '(none)'}")


def cmd_probe_stream(args) -> int:
    print(f"bluray-to-mp4 probe-stream {PROBE_ID} (app v{__version__})")
    """Probe the MakeMKV stream server against a real disc (v0.2.17 §1.2).

    Scans the disc, starts the stream server, probes the recommended
    title's URL with ffprobe (falling back to a short ffmpeg null read),
    prints a plain-text report plus a GREEN/YELLOW/RED verdict, and
    stops the server in every path. Exit codes: 0 GREEN/YELLOW, 1 RED,
    2 operational errors (no drive/disc/makemkvcon).
    """
    from .streamsource import (
        StreamServerError,
        probe_title_url,
        start_stream_server,
    )

    print(f"Scanning disc {args.index} ...")
    try:
        disc = scan_disc(args.index)
    except Exception as exc:  # MakeMkvError family: no makemkvcon/disc
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not disc.titles:
        print("error: the disc scan found no titles.", file=sys.stderr)
        return 2

    if args.title is not None:
        title = disc.title(args.title)
        if title is None:
            print(f"error: title {args.title} is not on this disc "
                  f"(scan found titles {', '.join(str(t.tid) for t in disc.titles)}).",
                  file=sys.stderr)
            return 2
    else:
        title = main_feature(disc) or max(
            disc.titles, key=lambda t: (t.duration_seconds, -t.tid))

    print(f"Disc: {disc.name} ({disc.media_type or 'unknown'} type)")
    print(f"Probing title {title.tid} "
          f"({_fmt_duration(title.duration_seconds)}, "
          f"{title.chapters} chapters)")

    server = None
    try:
        print("Starting stream server ...")
        try:
            server = start_stream_server(args.index)
        except StreamServerError as exc:
            # Server misbehaves: RED, not an operational error - the
            # lifecycle machinery is the thing under test here. probe.4:
            # every argument variant that exited before ready is printed
            # with its argv shape and drained stdout/stderr tails.
            print()
            _print_stream_attempts(exc.attempts)
            print(f"error: {exc}", file=sys.stderr)
            print("Verdict: RED - the stream server misbehaved "
                  "(failed to start or become ready); attach this "
                  "report to the milestone issue")
            return 1
        except MakeMkvError as exc:
            # makemkvcon missing / beta key / disc won't open: the
            # probe never got to test the server - operational, exit 2.
            print(f"error: {exc}", file=sys.stderr)
            return 2
        # probe.4: variants that died before the winning shape, then
        # which argument variant actually went ready.
        _print_stream_attempts(server.attempts)
        print(f"Stream server ready via variant {server.variant}")
        url = probe_title_url(server, title.tid)
        probe = _probe_url(url, args.timeout)

        print(f"Stream server: {server.base_url} (port {server.port})")
        print(f"Title URL: {url}")
        print(f"Opened by: {probe.prober}")
        if probe.ok:
            videos = [c for t, c in probe.streams if t == "video"]
            audios = [c for t, c in probe.streams if t == "audio"]
            subs = [c for t, c in probe.streams if t == "subtitle"]
            print(f"Container: {probe.format_name or 'unknown'}")
            if probe.duration is None:
                print("Duration: not reported by the server "
                      "(live-stream-like; seeking may be limited)")
            else:
                delta = probe.duration - title.duration_seconds
                print(f"Duration: {probe.duration:.1f}s vs scanned "
                      f"{title.duration_seconds}s "
                      f"(delta {delta:+.1f}s)")
            print(f"Video: {len(videos)} stream(s) - "
                  f"{', '.join(videos) or 'none'}")
            print(f"Audio: {len(audios)} stream(s) - "
                  f"{', '.join(audios) or 'none'}")
            print(f"Subtitles: {len(subs)} stream(s) - "
                  f"{', '.join(subs) or 'none'}")
            if videos and audios and subs:
                verdict = "GREEN"
                print("Verdict: GREEN - title URL streams video, audio, "
                      "AND subtitles; fast mode is viable")
            elif videos and audios:
                verdict = "YELLOW"
                print("Verdict: YELLOW - title URL streams video and "
                      "audio but NO subtitle track; fast mode needs the "
                      "subtitle-extraction fallback")
            else:
                verdict = "RED"
                missing = "no video" if not videos else "no audio"
                print(f"Verdict: RED - the title URL opened but serves "
                      f"{missing} stream(s); it is not usable for "
                      "encoding")
        else:
            verdict = "RED"
            print(f"Prober output: {probe.stderr_excerpt or '(none)'}")
            print("Verdict: RED - the title URL could not be opened by "
                  "ffprobe or ffmpeg; fast mode is descoped, attach "
                  "this report to the milestone issue")
        tail = " | ".join(server.stdout_tail)
        if tail:
            print(f"Server stdout (tail): {_excerpt(tail)}")
        # probe.4: stderr is captured now too - the real makemkvcon
        # error line may live only there.
        errtail = " | ".join(server.stderr_tail)
        if errtail:
            print(f"Server stderr (tail): {_excerpt(errtail)}")
    finally:
        if server is not None:
            server.stop()
            print("Stream server stopped.")
    return 0 if verdict in ("GREEN", "YELLOW") else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bluray-to-mp4",
        description="Rip personal Blu-ray/DVD discs to MP4 with smart track choices.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("drives", help="list optical drives and loaded discs").set_defaults(func=cmd_drives)

    p_scan = sub.add_parser("scan", help="scan a disc and show recommendations")
    p_scan.add_argument("index", type=int, help="disc index from `drives`")
    p_scan.set_defaults(func=cmd_scan)

    p_run = sub.add_parser("run", help="full pipeline: rip, encode, name")
    p_run.add_argument("index", type=int, help="disc index from `drives`")
    p_run.add_argument("--out", required=True, help="output directory")
    p_run.add_argument("--dry-run", action="store_true",
                        help="show the plan without ripping or encoding")
    p_run.add_argument("--encoder", choices=["auto", "nvenc", "x265"], default="auto")
    p_run.add_argument("--series-name", help="override series/show name")
    p_run.add_argument("--season", type=int, default=1)
    p_run.add_argument("--start-episode", type=int, default=1)
    p_run.add_argument("--year", type=int, help="movie year for naming")
    p_run.add_argument("--tmdb-key", default="",
                        help="TMDB v3 API key for episode-title/year autofill "
                             "(or set B2M_TMDB_KEY)")
    p_run.set_defaults(func=cmd_run)

    p_probe = sub.add_parser(
        "probe-stream",
        help="verify the MakeMKV stream server against a real disc "
             "(fast-mode feature probe)",
    )
    p_probe.add_argument("index", type=int, help="disc index from `drives`")
    p_probe.add_argument("--title", type=int,
                         help="probe this title id instead of the "
                              "recommended/main-feature title")
    p_probe.add_argument("--timeout", type=float, default=60.0,
                         help="per-probe timeout in seconds (default: 60)")
    p_probe.set_defaults(func=cmd_probe_stream)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nAborted.")
        return 130
    except Exception as exc:  # surface a clean message instead of a traceback
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())