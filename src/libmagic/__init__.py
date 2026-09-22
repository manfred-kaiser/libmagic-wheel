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

__all__ = [
    "UPSTREAM_VERSION",
    "Classification",
    "Magic",
    "MagicError",
    "bundled_default_mgc",
    "bundled_magdir",
    "compile_database",
]
