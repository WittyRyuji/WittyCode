"""Runtime guard: WittyCode is locked to Python 3.11.x.

Import this module first in every entry point; it aborts the process with a
clear message on any other Python version. See docs/phase0b-context-cache.md
(section D) for how the lock is enforced and how to recreate the venv.
"""

import sys

EXPECTED = (3, 11)

if sys.version_info[:2] != EXPECTED:
    raise SystemExit(
        "WittyCode requires Python {}.{}.x, but this interpreter is {}. "
        "Recreate the venv with: py -{}.{} -m venv .venv".format(
            EXPECTED[0], EXPECTED[1], sys.version.split()[0], EXPECTED[0], EXPECTED[1]
        )
    )