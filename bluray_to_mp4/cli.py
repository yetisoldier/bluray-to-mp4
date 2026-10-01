"""Command-line interface: drives | scan | run."""

from __future__ import annotations

import argparse
import sys

from .makemkv import find_drives, scan_disc
from .recommend import recommend_title
from .series import detect_series, main_feature

__all__ = ["main", "build_parser"]


def _fmt_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


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
        kind, tid, frac = update
        bar = "#" * int(frac * 40)
        print(
            f"\r  title {tid} {kind}: [{bar:<40}] {frac * 100:5.1f}%",
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