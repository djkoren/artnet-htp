"""ArtNet HTP merger package.

Version is read from installed package metadata (pyproject.toml is the single
source of truth). Falls back to "0.0.0+dev" if the package isn't installed
(e.g., running directly from a source tree without `pip install -e .`).
"""

from __future__ import annotations

try:
    from importlib.metadata import PackageNotFoundError, version as _pkg_version
    try:
        __version__ = _pkg_version("artnet-htp")
    except PackageNotFoundError:
        __version__ = "0.0.0+dev"
except ImportError:  # pragma: no cover — Python <3.8 won't reach here, we require 3.11
    __version__ = "0.0.0+dev"
