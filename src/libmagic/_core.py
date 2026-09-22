"""Minimal, dependency-free ctypes binding to a bundled libmagic.so.

Deliberately not built on top of the `python-magic` package: that package
resolves its shared library via `ctypes.util.find_library()` at import
time, which depends on the library being discoverable through OS search
paths (LD_LIBRARY_PATH / ldconfig) -- exactly what bundling our own
version-matched libmagic.so is meant to avoid. Loading the bundled file by
its absolute path (resolved via importlib.resources) sidesteps that
entirely, with no environment-variable or monkeypatch dependency.
"""

from __future__ import annotations

import ctypes
import importlib.resources
import os
import shutil
import tempfile
import threading
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable

# Flag values verified against src/magic.h.in in the file/file source tree
# (tag FILE5_48) -- see the project README for how these were confirmed.
MAGIC_NONE = 0x0000000
MAGIC_COMPRESS = 0x0000004
MAGIC_MIME_TYPE = 0x0000010
MAGIC_ERROR = 0x0000200
MAGIC_EXTENSION = 0x1000000
MAGIC_NO_COMPRESS_FORK = 0x4000000


class MagicError(OSError):
    """Raised for any libmagic-reported error (wraps magic_error())."""


def _decode(data: bytes) -> str:
    """Decode bytes from libmagic, never raising on unexpected content.

    libmagic can return metadata in the charset of the classified file
    itself (e.g. a Word doc's title), which is unknown to us and not
    necessarily valid UTF-8 -- python-magic hit this in the wild and
    works around it the same way, see its maybe_decode().
    """
    return data.decode("utf-8", errors="backslashreplace")


class _Cookie:
    """Owns one magic_t handle; closes it when garbage collected.

    Wrapping the raw int this way -- rather than relying only on explicit
    magic_close() calls at each call site -- is what actually prevents
    cookie leaks: threading.local drops its per-thread value when a thread
    exits, but a bare int has no way to notice that, so nothing would ever
    call magic_close() for it. A thread pool that cycles worker threads
    (gunicorn/uwsgi, a shrinking Celery pool -- ordinary under real load)
    would otherwise leak one native handle per thread that ever touched
    this Magic instance, for the life of the process. CPython's
    refcounting makes this deterministic: __del__ runs the moment the last
    reference (self._local.cookie, or the thread-local slot itself) goes
    away, not at some later, unpredictable GC pass.
    """

    __slots__ = ("value",)

    def __init__(self, value: int) -> None:
        self.value = value

    def __del__(self) -> None:
        # Guarded, not a bare call: at interpreter shutdown, CPython clears
        # module globals in an order that isn't guaranteed relative to
        # __del__ calls -- a well-known general Python gotcha, not specific
        # to this class. `_lib` (or its bound method) can already be gone
        # by the time this runs for a cookie that outlives most other
        # cleanup. Silently skipping the close in that narrow window is
        # fine: the process is exiting anyway and the OS reclaims the
        # handle regardless.
        if _lib is not None:
            _lib.magic_close(self.value)


class _ThreadState(threading.local):
    """Typed per-thread storage -- avoids untyped getattr()/Any at the mypy level."""

    cookie: _Cookie | None = None
    mtime: int | None = None


_DATA_PACKAGE = f"{__package__}.data"


def _load_bundled_lib() -> ctypes.CDLL:
    """Load the package's own bundled libmagic.so by absolute path."""
    stack = ExitStack()
    lib_path = stack.enter_context(
        importlib.resources.as_file(
            importlib.resources.files(_DATA_PACKAGE) / "libmagic.so.1",
        ),
    )
    lib = ctypes.CDLL(str(lib_path))

    lib.magic_open.restype = ctypes.c_void_p
    lib.magic_open.argtypes = [ctypes.c_int]
    lib.magic_close.argtypes = [ctypes.c_void_p]
    lib.magic_load.restype = ctypes.c_int
    lib.magic_load.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.magic_compile.restype = ctypes.c_int
    lib.magic_compile.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.magic_setflags.restype = ctypes.c_int
    lib.magic_setflags.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.magic_error.restype = ctypes.c_char_p
    lib.magic_error.argtypes = [ctypes.c_void_p]
    lib.magic_buffer.restype = ctypes.c_char_p
    lib.magic_buffer.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
    lib.magic_file.restype = ctypes.c_char_p
    lib.magic_file.argtypes = [ctypes.c_void_p, ctypes.c_char_p]

    return lib


_lib = _load_bundled_lib()


def bundled_magdir() -> Path:
    """Directory holding the bundled, unmodified upstream Magdir fragments."""
    stack = ExitStack()
    return stack.enter_context(
        importlib.resources.as_file(importlib.resources.files(_DATA_PACKAGE) / "magdir"),
    )


def bundled_default_mgc() -> Path:
    """Return the unmodified-Magdir .mgc bundled for development/debugging use.

    Not meant for real deployments -- those should compile their own
    combined database (with their own overrides) via compile_database().
    """
    stack = ExitStack()
    return stack.enter_context(
        importlib.resources.as_file(importlib.resources.files(_DATA_PACKAGE) / "default.mgc"),
    )


def compile_database(
    overrides: dict[str, Path] | None = None,
    extra: list[Path] | None = None,
    output_dir: Path | None = None,
    output_name: str = "combined",
) -> Path:
    """Compile a .mgc from the bundled upstream Magdir plus overrides/extras.

    overrides: fragment name -> replacement file. The name must match a
               bundled fragment's filename, or this raises ValueError --
               a typo here should fail loudly, not silently no-op.
    extra:     additional fragment files with no upstream counterpart.
    """
    overrides = overrides or {}
    extra = extra or []
    magdir = bundled_magdir()
    known = {f.name for f in magdir.iterdir()}
    unknown = set(overrides) - known
    if unknown:
        message = f"unknown Magdir fragment name(s): {sorted(unknown)}"
        raise ValueError(message)

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / output_name
        staging.mkdir()
        for fragment in magdir.iterdir():
            shutil.copy(overrides.get(fragment.name, fragment), staging / fragment.name)
        for fragment in extra:
            shutil.copy(fragment, staging / fragment.name)

        cookie = _lib.magic_open(MAGIC_NONE)
        if cookie is None:
            message = "magic_open() failed"
            raise MagicError(message)
        try:
            cwd = Path.cwd()
            os.chdir(staging.parent)
            try:
                if _lib.magic_compile(cookie, str(staging).encode()) != 0:
                    raise MagicError(_decode(_lib.magic_error(cookie)))
                produced = staging.parent / f"{output_name}.mgc"
            finally:
                os.chdir(cwd)
        finally:
            _lib.magic_close(cookie)

        dest_dir = output_dir or Path.cwd()
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{output_name}.mgc"
        shutil.move(str(produced), str(dest))
        return dest


@dataclass(frozen=True)
class Classification:
    """A single classification result: three independent libmagic queries."""

    description: str
    mime_type: str
    extensions: str


class Magic:
    """Thread-safe, auto-reloading libmagic wrapper.

    One instance per process, created once and reused (magic_load() is the
    expensive step). Every thread keeps its own cookie in thread-local
    storage: this is what makes concurrent use safe (libmagic cookies are
    not safe to share across threads) *and* what makes hot-reloading the
    .mgc file safe without locks -- a cookie is only ever closed by the
    one thread that exclusively owned it, so there is no "closed while
    another thread is still using it" race by construction. See the
    project README for the deployment-side requirement (atomic replace)
    this relies on.
    """

    def __init__(
        self,
        magic_file: str,
        *,
        uncompress: bool = False,
        allow_compress_fork: bool = False,
    ) -> None:
        """Configure the database path and flags; nothing is loaded yet.

        allow_compress_fork: libmagic's MAGIC_NO_COMPRESS_FORK is not a
        selective "block only external tools" switch -- verified against
        compress.c: it blocks ALL decompression uniformly, including
        formats with a builtin, fork-free, in-process decompressor (e.g.
        zlib/gzip). With uncompress=True and this left False (the safe
        default), no fork ever happens, but no decompression happens
        either -- MAGIC_COMPRESS is effectively a no-op that reports a
        "Fork is required to uncompress, but disabled" error instead of a
        real classification. Set this True only if the calling process
        already has its own OS-level sandboxing (e.g. a systemd-hardened
        service, per the README) -- a forked decompressor then inherits
        that same sandbox rather than running fully unconstrained.
        """
        self._path = Path(magic_file)
        flags = MAGIC_ERROR
        if uncompress:
            flags |= MAGIC_COMPRESS
            if not allow_compress_fork:
                flags |= MAGIC_NO_COMPRESS_FORK
        self._base_flags = flags
        self._local = _ThreadState()

    def _open_cookie(self) -> _Cookie:
        raw = cast("int | None", _lib.magic_open(self._base_flags))
        if raw is None:
            message = "magic_open() failed"
            raise MagicError(message)
        if _lib.magic_load(raw, str(self._path).encode()) != 0:
            err = _decode(_lib.magic_error(raw))
            _lib.magic_close(raw)
            message = f"magic_load({self._path}) failed: {err}"
            raise MagicError(message)
        return _Cookie(raw)

    def _current_cookie(self) -> _Cookie:
        cookie = self._local.cookie
        try:
            mtime = self._path.stat().st_mtime_ns
        except FileNotFoundError:
            if cookie is not None:
                # Transiently missing (e.g. mid atomic-rename elsewhere) --
                # keep serving the last known-good database rather than
                # failing a classification over it.
                return cookie
            message = f"magic database not found: {self._path}"
            raise MagicError(message) from None

        if cookie is None or mtime != self._local.mtime:
            cookie = self._open_cookie()
            # The previous _Cookie (if any) is dropped here -- its __del__
            # closes the stale native handle immediately (see _Cookie's
            # docstring), no separate manual magic_close() call needed.
            self._local.cookie = cookie
            self._local.mtime = mtime
        return cookie

    def _classify(self, cookie: _Cookie, extra_flags: int, call: Callable[[], bytes | None]) -> str:
        if _lib.magic_setflags(cookie.value, self._base_flags | extra_flags) != 0:
            raise MagicError(_decode(_lib.magic_error(cookie.value)))
        result = call()
        if result is None:
            raise MagicError(_decode(_lib.magic_error(cookie.value)))
        return _decode(result)

    def from_buffer(self, data: bytes) -> Classification:
        """Classify raw bytes already held in memory."""
        cookie = self._current_cookie()

        def call() -> bytes | None:
            return cast("bytes | None", _lib.magic_buffer(cookie.value, data, len(data)))

        return Classification(
            description=self._classify(cookie, MAGIC_NONE, call),
            mime_type=self._classify(cookie, MAGIC_MIME_TYPE, call),
            extensions=self._classify(cookie, MAGIC_EXTENSION, call),
        )

    def from_file(self, path: str) -> Classification:
        """Classify a file by path."""
        cookie = self._current_cookie()
        # surrogateescape, not the plain default: Linux filenames are
        # arbitrary bytes, not guaranteed valid UTF-8 -- the same encoding
        # os.fsencode()/Path.iterdir() already use for exactly this reason,
        # so a path obtained from a directory listing round-trips instead
        # of raising UnicodeEncodeError.
        encoded_path = path.encode("utf-8", errors="surrogateescape")

        def call() -> bytes | None:
            return cast("bytes | None", _lib.magic_file(cookie.value, encoded_path))

        return Classification(
            description=self._classify(cookie, MAGIC_NONE, call),
            mime_type=self._classify(cookie, MAGIC_MIME_TYPE, call),
            extensions=self._classify(cookie, MAGIC_EXTENSION, call),
        )
