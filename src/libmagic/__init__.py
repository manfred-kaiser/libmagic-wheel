"""Bundled, version-pinned upstream libmagic -- see the project README."""

from ._core import (
    Classification,
    Magic,
    MagicError,
    bundled_default_mgc,
    bundled_magdir,
    compile_database,
)
from ._upstream_version import UPSTREAM_VERSION

__version__ = "0.2.0"

__all__ = [
    "UPSTREAM_VERSION",
    "Classification",
    "Magic",
    "MagicError",
    "__version__",
    "bundled_default_mgc",
    "bundled_magdir",
    "compile_database",
]
