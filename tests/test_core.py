"""Tests for libmagic._core."""

from __future__ import annotations

import gzip
import os
import time
from typing import TYPE_CHECKING

import pytest

from libmagic import (
    UPSTREAM_VERSION,
    Classification,
    Magic,
    MagicError,
    _core,
    bundled_default_mgc,
    bundled_magdir,
    compile_database,
)

from .conftest import PDF_BYTES, TEXT_BYTES

if TYPE_CHECKING:
    from pathlib import Path


def test_bundled_magdir_contains_upstream_fragments() -> None:
    magdir = bundled_magdir()
    names = {f.name for f in magdir.iterdir()}
    assert "pgp" in names
    assert "elf" in names


def test_bundled_default_mgc_is_loadable() -> None:
    mgc = bundled_default_mgc()
    assert mgc.exists()
    m = Magic(magic_file=str(mgc))
    result = m.from_buffer(PDF_BYTES)
    assert result.mime_type == "application/pdf"


def test_magic_without_magic_file_uses_bundled_default() -> None:
    m = Magic()
    result = m.from_buffer(PDF_BYTES)
    assert result.mime_type == "application/pdf"


def test_cookie_del_guards_against_lib_being_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulates the narrow interpreter-shutdown window where module globals
    # can already be cleared before a lingering _Cookie's __del__ runs --
    # calling __del__ directly (a plain method, nothing stops that) tests
    # it deterministically instead of relying on GC/shutdown timing.
    cookie = _core._Cookie(1)
    monkeypatch.setattr(_core, "_lib", None)
    cookie.__del__()  # must not raise even though _lib is gone


def test_upstream_version_is_exposed() -> None:
    # Format-checked, not value-checked: the actual value tracks whatever
    # scripts/build-lib.sh has pinned, deliberately not hardcoded here.
    assert UPSTREAM_VERSION
    assert UPSTREAM_VERSION[0].isdigit()


def test_compile_database_unmodified(compiled_mgc: Path) -> None:
    assert compiled_mgc.exists()
    assert compiled_mgc.stat().st_size > 0


def test_compile_database_rejects_unknown_override(tmp_path: Path) -> None:
    bogus = tmp_path / "gpg"
    bogus.write_text("# not a real Magdir fragment name\n")
    with pytest.raises(ValueError, match="unknown Magdir fragment"):
        compile_database(overrides={"gpg": bogus}, output_dir=tmp_path)


def test_compile_database_replaces_known_fragment(tmp_path: Path) -> None:
    # PDF detection lives in Magdir's "pdf" fragment specifically -- replacing
    # "pgp" would leave PDF matching untouched, so override the fragment that
    # actually owns the pattern under test.
    replacement = tmp_path / "pdf"
    replacement.write_text("0 string %PDF replaced-pdf-fragment\n")
    mgc = compile_database(
        overrides={"pdf": replacement}, output_dir=tmp_path, output_name="replaced",
    )
    m = Magic(magic_file=str(mgc))
    result = m.from_buffer(PDF_BYTES)
    # the replacement fragment's own description text, not upstream's PDF rule
    assert result.description == "replaced-pdf-fragment"


def test_compile_database_adds_extra_fragment(tmp_path: Path) -> None:
    extra = tmp_path / "my-custom-fragment"
    extra.write_text("0 string \\xCA\\xFE custom-magic-match\n")
    mgc = compile_database(extra=[extra], output_dir=tmp_path, output_name="extra")
    m = Magic(magic_file=str(mgc))
    result = m.from_buffer(b"\xca\xfe rest of file")
    assert result.description == "custom-magic-match"


def test_compile_database_default_output_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    mgc = compile_database(output_name="cwd-default")
    assert mgc == tmp_path / "cwd-default.mgc"


def test_compile_database_propagates_compile_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_core._lib, "magic_compile", lambda _cookie, _path: -1)
    monkeypatch.setattr(_core._lib, "magic_error", lambda _cookie: b"boom")
    with pytest.raises(MagicError, match="boom"):
        compile_database(output_dir=tmp_path)


def test_compile_database_open_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_core._lib, "magic_open", lambda _flags: None)
    with pytest.raises(MagicError, match="magic_open"):
        compile_database(output_dir=tmp_path)


def test_from_buffer_returns_classification(compiled_mgc: Path) -> None:
    m = Magic(magic_file=str(compiled_mgc))
    result = m.from_buffer(PDF_BYTES)
    assert isinstance(result, Classification)
    assert result.mime_type == "application/pdf"
    assert "pdf" in result.extensions


def test_from_buffer_plain_text(compiled_mgc: Path) -> None:
    m = Magic(magic_file=str(compiled_mgc))
    result = m.from_buffer(TEXT_BYTES)
    assert result.mime_type.startswith("text/")


def test_from_buffer_empty(compiled_mgc: Path) -> None:
    m = Magic(magic_file=str(compiled_mgc))
    result = m.from_buffer(b"")
    assert isinstance(result, Classification)
    assert result.description  # some non-empty classification, not a crash


def test_from_buffer_single_byte(compiled_mgc: Path) -> None:
    m = Magic(magic_file=str(compiled_mgc))
    result = m.from_buffer(b"\x00")
    assert isinstance(result, Classification)


def test_from_buffer_large(compiled_mgc: Path) -> None:
    # 8 MiB of repeated plain text -- larger than any single internal
    # libmagic read-limit default, proving nothing chokes on size alone.
    data = (TEXT_BYTES * 400_000)[: 8 * 1024 * 1024]
    m = Magic(magic_file=str(compiled_mgc))
    result = m.from_buffer(data)
    assert result.mime_type.startswith("text/")


def test_from_buffer_all_null_bytes(compiled_mgc: Path) -> None:
    m = Magic(magic_file=str(compiled_mgc))
    result = m.from_buffer(b"\x00" * 4096)
    assert isinstance(result, Classification)


def test_uncompress_without_allow_compress_fork_never_decompresses(
    compiled_mgc: Path,
) -> None:
    # Verified against compress.c: MAGIC_NO_COMPRESS_FORK blocks ALL
    # decompression uniformly, including builtin, fork-free decompressors
    # like zlib -- not just fork-requiring ones. The safe default
    # (allow_compress_fork=False) must therefore *never* actually
    # decompress, only fail cleanly and say why -- see Magic.__init__'s
    # docstring and the README's security notes for the full reasoning.
    gzipped = gzip.compress(b"hello world, this is the payload\n" * 20)

    m = Magic(magic_file=str(compiled_mgc), uncompress=True)
    result = m.from_buffer(gzipped)

    assert "Fork is required to uncompress, but disabled" in result.description
    assert "decompression-error" in result.mime_type


def test_uncompress_with_allow_compress_fork_actually_decompresses(
    compiled_mgc: Path,
) -> None:
    inner = b"hello world, this is the payload\n" * 20
    gzipped = gzip.compress(inner)

    m = Magic(magic_file=str(compiled_mgc), uncompress=True, allow_compress_fork=True)
    result = m.from_buffer(gzipped)

    # The *inner* content's type, not the outer gzip wrapper's -- proves
    # real decompression happened, not just accepted-without-erroring.
    assert result.description.startswith("ASCII text")
    assert result.mime_type == "text/plain"


def test_from_file(compiled_mgc: Path, tmp_path: Path) -> None:
    sample = tmp_path / "sample.pdf"
    sample.write_bytes(PDF_BYTES)
    m = Magic(magic_file=str(compiled_mgc))
    result = m.from_file(str(sample))
    assert result.mime_type == "application/pdf"


def test_from_file_with_non_utf8_path(compiled_mgc: Path, tmp_path: Path) -> None:
    # Linux filenames are arbitrary bytes, not guaranteed valid UTF-8.
    # os.fsdecode() round-trips an invalid byte through a surrogate
    # character, exactly what encoded_path's surrogateescape must accept
    # without raising UnicodeEncodeError.
    bad_name = os.fsdecode(b"weird-\xff-name.pdf")
    sample = tmp_path / bad_name
    sample.write_bytes(PDF_BYTES)

    m = Magic(magic_file=str(compiled_mgc))
    result = m.from_file(str(sample))

    assert result.mime_type == "application/pdf"


def test_from_buffer_decodes_non_utf8_metadata_without_crashing(
    compiled_mgc: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # libmagic can return description text in the charset of the classified
    # file itself (e.g. a Word doc's title) -- not necessarily valid UTF-8.
    # backslashreplace must keep this from raising UnicodeDecodeError.
    monkeypatch.setattr(_core._lib, "magic_buffer", lambda *_a, **_k: b"bad-\xff-utf8")
    m = Magic(magic_file=str(compiled_mgc))

    result = m.from_buffer(TEXT_BYTES)

    assert result.description == "bad-\\xff-utf8"


def test_uncompress_flag_sets_compress_and_nofork(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_flags: list[int] = []

    def _fake_magic_open(flags: int) -> int:
        seen_flags.append(flags)
        return 1

    monkeypatch.setattr(_core._lib, "magic_open", _fake_magic_open)
    monkeypatch.setattr(_core._lib, "magic_load", lambda _cookie, _path: 0)
    # The fake handle (1) is not a real magic_t -- _Cookie.__del__ will try
    # to magic_close() it once this goes out of scope, which must not reach
    # the real native function with a bogus value. Mock that too.
    monkeypatch.setattr(_core._lib, "magic_close", lambda _cookie: None)

    Magic(magic_file="irrelevant.mgc", uncompress=True)._open_cookie()

    assert seen_flags[-1] & _core.MAGIC_COMPRESS
    assert seen_flags[-1] & _core.MAGIC_NO_COMPRESS_FORK


def test_allow_compress_fork_omits_nofork_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_flags: list[int] = []

    def _fake_magic_open(flags: int) -> int:
        seen_flags.append(flags)
        return 1

    monkeypatch.setattr(_core._lib, "magic_open", _fake_magic_open)
    monkeypatch.setattr(_core._lib, "magic_load", lambda _cookie, _path: 0)
    monkeypatch.setattr(_core._lib, "magic_close", lambda _cookie: None)

    Magic(magic_file="irrelevant.mgc", uncompress=True, allow_compress_fork=True)._open_cookie()

    assert seen_flags[-1] & _core.MAGIC_COMPRESS
    assert not seen_flags[-1] & _core.MAGIC_NO_COMPRESS_FORK


def test_missing_database_raises_clearly(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.mgc"
    m = Magic(magic_file=str(missing))
    with pytest.raises(MagicError, match="magic database not found"):
        m.from_buffer(TEXT_BYTES)


def test_transiently_missing_database_keeps_serving_cached_cookie(
    compiled_mgc: Path, tmp_path: Path,
) -> None:
    live = tmp_path / "live.mgc"
    live.write_bytes(compiled_mgc.read_bytes())
    m = Magic(magic_file=str(live))
    m.from_buffer(TEXT_BYTES)  # establishes a cached cookie
    cached_cookie = m._local.cookie

    live.unlink()  # simulate a transient gap mid atomic-rename elsewhere
    result = m.from_buffer(TEXT_BYTES)

    assert result.mime_type.startswith("text/")
    assert m._local.cookie == cached_cookie


def test_open_cookie_rejects_invalid_database(tmp_path: Path) -> None:
    garbage = tmp_path / "garbage.mgc"
    garbage.write_bytes(b"this is not a compiled magic database\n")
    m = Magic(magic_file=str(garbage))
    with pytest.raises(MagicError, match="magic_load"):
        m.from_buffer(TEXT_BYTES)


def test_open_cookie_open_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    existing = tmp_path / "exists.mgc"
    existing.write_bytes(b"not actually loaded, magic_open is mocked first")
    monkeypatch.setattr(_core._lib, "magic_open", lambda _flags: None)
    m = Magic(magic_file=str(existing))
    with pytest.raises(MagicError, match="magic_open"):
        m.from_buffer(TEXT_BYTES)


def test_classify_setflags_failure(compiled_mgc: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    m = Magic(magic_file=str(compiled_mgc))
    m._current_cookie()
    monkeypatch.setattr(_core._lib, "magic_setflags", lambda _cookie, _flags: -1)
    monkeypatch.setattr(_core._lib, "magic_error", lambda _cookie: b"setflags failed")
    with pytest.raises(MagicError, match="setflags failed"):
        m.from_buffer(TEXT_BYTES)


def test_classify_call_returns_none(compiled_mgc: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    m = Magic(magic_file=str(compiled_mgc))
    m._current_cookie()
    monkeypatch.setattr(_core._lib, "magic_buffer", lambda _cookie, _buf, _len: None)
    monkeypatch.setattr(_core._lib, "magic_error", lambda _cookie: b"buffer failed")
    with pytest.raises(MagicError, match="buffer failed"):
        m.from_buffer(TEXT_BYTES)


def test_reload_on_mtime_change(compiled_mgc: Path, tmp_path: Path) -> None:
    live = tmp_path / "live.mgc"
    live.write_bytes(compiled_mgc.read_bytes())
    m = Magic(magic_file=str(live))
    m.from_buffer(TEXT_BYTES)
    first_cookie = m._local.cookie

    # simulate an atomic update: new content, new mtime, delivered via rename
    time.sleep(0.01)
    tmp = live.with_suffix(".tmp")
    tmp.write_bytes(compiled_mgc.read_bytes())
    tmp.replace(live)

    m.from_buffer(TEXT_BYTES)
    second_cookie = m._local.cookie
    assert second_cookie != first_cookie


def test_no_reload_when_mtime_unchanged(compiled_mgc: Path) -> None:
    m = Magic(magic_file=str(compiled_mgc))
    m.from_buffer(TEXT_BYTES)
    first_cookie = m._local.cookie
    m.from_buffer(TEXT_BYTES)
    assert m._local.cookie == first_cookie
