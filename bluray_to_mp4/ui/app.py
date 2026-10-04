"""UI entry point: start the monitor per settings, then the desktop window."""

from __future__ import annotations

import signal
import sys
from pathlib import Path


def main() -> int:
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from ..config import load_settings
    from ..monitor import start_server_with_fallback
    from .main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("bluray-to-mp4")
    icon_path = Path(__file__).with_name("app_icon.svg")
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))

    settings = load_settings()
    if settings.monitor_enabled:
        try:
            _, _, actual_port = start_server_with_fallback(
                host=settings.monitor_host,
                port=settings.monitor_port,
                token=settings.monitor_token,
            )
            if actual_port != settings.monitor_port:
                settings.monitor_port = actual_port
                settings.save()
            # DR7: console shows the monitor URL on startup (BUILD.md promise).
            from ..monitor import lan_ip
            host = lan_ip() if settings.monitor_host == "0.0.0.0" else settings.monitor_host
            print(
                f"[monitor] http://{host}:{actual_port}/status "
                f"(Bearer: {settings.monitor_token})",
                flush=True,
            )
        except OSError as exc:
            print(f"[monitor] not started ({exc})", flush=True)

    window = MainWindow(settings)
    window.show()
    signal.signal(signal.SIGINT, signal.SIG_DFL)  # Ctrl-C works during dev
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())