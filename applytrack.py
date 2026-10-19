"""Entry point for running ApplyTrack from a checkout: ``python3 applytrack.py --help``.

The code lives in the ``applytrack`` package next to this file, which Python
imports in preference to this script. ``python3 -m applytrack`` is equivalent.
"""

from applytrack.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
