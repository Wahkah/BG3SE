"""Entry point used by PyInstaller to produce the standalone app."""

import multiprocessing
import sys

from bgse.app import main

if __name__ == "__main__":
    # Required so frozen builds do not re-launch the window in child processes.
    multiprocessing.freeze_support()
    sys.exit(main())
