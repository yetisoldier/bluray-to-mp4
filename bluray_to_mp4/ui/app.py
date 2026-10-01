"""UI entry point: start the monitor, then the desktop window."""

from __future__ import annotations

import signal
import sys


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from ..config import load_settings
    from ..monitor import start_server
    from .main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("bluray-to-mp4")

    settings = load_settings()
    if settings.monitor_enabled:
        try:
            _, base_url = start_server(
                host=settings.monitor_host,
                port=settings.monitor_port,
                token=settings.monitor_token,
            )
            print(f"[monitor] {base_url}/status", flush=True)
        except OSError as exc:
            print(f"[monitor] not started ({exc})", flush=True)

    window = MainWindow(settings)
    window.show()
    signal.signal(signal.SIGINT, signal.SIG_DFL)  # Ctrl-C works during dev
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())