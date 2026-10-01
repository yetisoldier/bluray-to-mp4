"""PyInstaller entry point.

The frozen exe must not run bluray_to_mp4/__main__.py as a plain script:
its relative imports only resolve inside the package. Importing it as a
module (like `python -m` does) keeps every import path intact.
"""

import sys

from bluray_to_mp4.__main__ import main

if __name__ == "__main__":
    sys.exit(main())