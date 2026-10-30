"""Make this directory importable without shadowing the rest of the repository.

Run these checks in their own pytest process. A shared process with e2e/ would bind
the names runner and targets to the wrong modules.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for entry in (str(ROOT), str(HERE)):
    if entry not in sys.path:
        sys.path.insert(0, entry)
