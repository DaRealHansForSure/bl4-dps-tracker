"""
Third-party packages shipped inside the mod, because the game's embedded Python has no
package manager. `use_vendored_packages()` puts this directory on sys.path; that works from
the .sdkmod zip as well, since zipimport accepts a path inside an archive.

- websockets 16.0 (BSD-3-Clause, LICENSE next to the package): the dashboard's HTTP and
  WebSocket server. Pure Python; the optional C speedups module is left out, the package
  falls back to its Python implementation.
"""

from __future__ import annotations

import sys
from pathlib import Path

VENDOR_DIR = Path(__file__).parent


def use_vendored_packages() -> None:
    """Make the vendored packages importable. Goes in front of sys.path so the copy that was
    tested with the mod wins over anything else on the path; a package that was already
    imported stays as it is."""
    path = str(VENDOR_DIR)
    if path not in sys.path:
        sys.path.insert(0, path)
