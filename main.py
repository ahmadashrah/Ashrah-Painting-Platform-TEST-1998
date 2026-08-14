"""Entrypoint for hosts that look for one.

Railway's Railpack builder detects Python, installs from `requirements.txt`,
and then looks for a start command. It does not read `railway.json`'s
builder field, and `requirements.txt` does not install this project — so
`python -m lumia.server` finds no module and the deploy fails before it
starts.

This file solves both halves. Railpack runs `main.py` when it finds one,
and putting `src` on the path here means the app boots whether or not the
package was installed. Locally it changes nothing:

    python main.py                 # same as python -m lumia.server
"""

from __future__ import annotations

import sys
from pathlib import Path

# Python block-buffers stdout when it is a pipe rather than a terminal, which
# is exactly what a platform log collector attaches. Without this, output
# appears minutes late or is lost entirely when the container stops — and the
# logs you most need are the ones from a deploy that died.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):  # pragma: no cover - unusual stream
        pass

# Ahead of site-packages: a checkout should run its own code, not a copy
# installed earlier from somewhere else.
SRC = Path(__file__).resolve().parent / "src"
if SRC.is_dir():
    sys.path.insert(0, str(SRC))

from lumia.server import main  # noqa: E402 - the path has to be set first

if __name__ == "__main__":
    raise SystemExit(main())
