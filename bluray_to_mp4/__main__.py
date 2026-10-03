"""Python entry: `python -m bluray_to_mp4` launches the GUI (or CLI args).

Usage:
    python -m bluray_to_mp4              -> desktop UI
    python -m bluray_to_mp4 <cli args>  -> same as the `bluray-to-mp4` CLI
    python -m bluray_to_mp4 --status    -> print current monitor snapshot
"""

from __future__ import annotations

import sys


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] in ("drives", "scan", "run", "probe-stream"):
        from .cli import main as cli_main
        return cli_main()
    if argv and argv[0] == "--status":
        from .monitor import state
        import json
        print(json.dumps(state.snapshot(), indent=2))
        return 0
    # Default: the desktop UI.
    try:
        from .ui.app import main as ui_main
    except ImportError as exc:
        print(
            "The desktop UI needs PySide6. Install it with:\n"
            "    pip install PySide6\n"
            "Or use the CLI: bluray-to-mp4 drives|scan|run\n"
            f"({exc})",
            file=sys.stderr,
        )
        return 1
    return ui_main()


if __name__ == "__main__":
    sys.exit(main())