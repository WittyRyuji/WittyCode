"""Entry point: ``python -m desktop``."""

from desktop import _pyguard  # noqa: F401  (first import: Python 3.11 guard)

import sys

from desktop.ui.app import main

if __name__ == "__main__":
    sys.exit(main(sys.argv))