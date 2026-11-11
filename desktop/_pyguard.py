"""Runtime guard: WittyCode is locked to Python 3.11.x.

``desktop/__init__.py`` imports this module first, so it runs before any
module that relies on 3.11 syntax or on the wheels pinned for 3.11 is loaded.
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