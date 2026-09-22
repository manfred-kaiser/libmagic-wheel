"""Tests for libmagic.cli."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from libmagic.cli import _split_override_dir, main

from .conftest import PDF_BYTES


def test_split_override_dir_separates_known_and_new(tmp_path: Path) -> None:
    (tmp_path / "pgp").write_text("# replaces a bundled fragment\n")
    (tmp_path / "our-custom-fragment").write_text("# a new fragment\n")
    (tmp_path / "a-subdirectory").mkdir()

    overrides, extra = _split_override_dir(tmp_path)

    assert set(overrides) == {"pgp"}
    assert [f.name for f in extra] == ["our-custom-fragment"]


def test_compile_without_override_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["compile", "--output-dir", str(tmp_path), "--name", "plain"])

    assert exit_code == 0
    assert (tmp_path / "plain.mgc").exists()
    out = capsys.readouterr().out
    assert "wrote" in out
    assert "replaced" not in out
    assert "added" not in out


def test_compile_with_override_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    override_dir = tmp_path / "overrides"
    override_dir.mkdir()
    (override_dir / "pgp").write_text("0 string %PDF replaced-by-cli\n")
    (override_dir / "brand-new").write_text("0 string ZZZZ new-by-cli\n")
    output_dir = tmp_path / "out"

    exit_code = main(
        [
            "compile",
            "--override-dir",
            str(override_dir),
            "--output-dir",
            str(output_dir),
            "--name",
            "withoverrides",
        ],
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "replaced 1 bundled fragment(s): ['pgp']" in out
    assert "added 1 new fragment(s): ['brand-new']" in out


def test_compile_rejects_missing_override_dir(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["compile", "--override-dir", str(tmp_path / "nope")])


def test_compile_with_rpm_reports_rpm_build_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    # "bad name!" is a valid .mgc filename stem but fails build_rpm()'s
    # stricter RPM-name allowlist -- exercises the --rpm error path
    # without needing to break rpmbuild itself.
    exit_code = main(
        ["compile", "--output-dir", str(tmp_path), "--name", "bad name!", "--rpm"],
    )

    assert exit_code == 1
    assert (tmp_path / "bad name!.mgc").exists()
    assert "RPM build failed" in capsys.readouterr().err


def test_classify_with_explicit_mgc(
    compiled_mgc: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    sample = tmp_path / "sample.pdf"
    sample.write_bytes(PDF_BYTES)

    exit_code = main(["classify", "--mgc", str(compiled_mgc), str(sample)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert f"{sample}: PDF document" in out
    assert "application/pdf" in out


def test_classify_without_mgc_uses_bundled_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    sample = tmp_path / "sample.pdf"
    sample.write_bytes(PDF_BYTES)

    exit_code = main(["classify", str(sample)])

    assert exit_code == 0
    assert "application/pdf" in capsys.readouterr().out


def test_classify_reports_per_file_errors_and_continues(
    compiled_mgc: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "does-not-exist.pdf"
    sample = tmp_path / "sample.pdf"
    sample.write_bytes(PDF_BYTES)

    exit_code = main(["classify", "--mgc", str(compiled_mgc), str(missing), str(sample)])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "ERROR" in captured.err
    assert "application/pdf" in captured.out


def test_rpm_builds_and_installs_to_the_given_path(
    compiled_mgc: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    output_dir = tmp_path / "out"

    exit_code = main(
        [
            "rpm",
            "--mgc",
            str(compiled_mgc),
            "--name",
            "libmagic-wheel-database-test",
            "--version",
            "1.0",
            "--install-path",
            "/etc/libmagic-wheel-test/combined.mgc",
            "--output-dir",
            str(output_dir),
        ],
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "wrote" in out
    rpms = list(output_dir.glob("*.rpm"))
    assert len(rpms) == 1

    listing = subprocess.run(
        ["rpm", "-qlp", str(rpms[0])], capture_output=True, text=True, check=True,
    ).stdout
    assert listing.strip() == "/etc/libmagic-wheel-test/combined.mgc"


def test_rpm_uses_current_utc_timestamp_when_version_omitted(
    compiled_mgc: Path, tmp_path: Path,
) -> None:
    output_dir = tmp_path / "out"
    exit_code = main(
        ["rpm", "--mgc", str(compiled_mgc), "--name", "libmagic-wheel-database-test",
         "--output-dir", str(output_dir)],
    )
    assert exit_code == 0
    rpms = list(output_dir.glob("*.rpm"))
    assert len(rpms) == 1

    version = subprocess.run(
        ["rpm", "-qp", "--qf", "%{VERSION}", str(rpms[0])],
        capture_output=True, text=True, check=True,
    ).stdout
    assert re.fullmatch(r"\d{12}", version)


def test_compile_with_rpm_derives_rpm_name_from_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        ["compile", "--output-dir", str(tmp_path), "--name", "combined", "--rpm"],
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert (tmp_path / "combined.mgc").exists()
    rpms = list(tmp_path.glob("combined-*.rpm"))
    assert len(rpms) == 1
    assert str(rpms[0]) in out


def test_rpm_rejects_missing_mgc(tmp_path: Path) -> None:
    exit_code = main(
        [
            "rpm",
            "--mgc",
            str(tmp_path / "nope.mgc"),
            "--name",
            "x",
            "--version",
            "1.0",
        ],
    )
    assert exit_code == 1


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--name", "bad name!"),
        ("--version", "1.0-bad"),
        ("--release", "1;rm -rf /"),
        ("--license", "line1\nline2"),
        ("--install-path", "relative/path.mgc"),
        ("--install-path", "/etc/../etc/passwd"),
    ],
)
def test_rpm_rejects_unsafe_values(
    compiled_mgc: Path, flag: str, value: str,
) -> None:
    exit_code = main(
        [
            "rpm",
            "--mgc",
            str(compiled_mgc),
            "--name",
            "libmagic-wheel-database-test",
            "--version",
            "1.0",
            flag,
            value,
        ],
    )
    assert exit_code == 1


def test_rpm_reports_missing_rpmbuild(
    compiled_mgc: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    exit_code = main(
        ["rpm", "--mgc", str(compiled_mgc), "--name", "x", "--version", "1.0"],
    )
    assert exit_code == 1


def test_rpm_reports_unsafe_resolved_source_path(
    compiled_mgc: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "resolve", lambda self: Path("/tmp/evil;rm"))  # noqa: ARG005, S108
    exit_code = main(
        ["rpm", "--mgc", str(compiled_mgc), "--name", "x", "--version", "1.0"],
    )
    assert exit_code == 1


def test_rpm_reports_rpmbuild_failure(
    compiled_mgc: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, returncode=1, stdout="", stderr="boom"),  # noqa: ARG005
    )
    exit_code = main(
        ["rpm", "--mgc", str(compiled_mgc), "--name", "x", "--version", "1.0"],
    )
    assert exit_code == 1


def test_rpm_reports_success_with_no_output_file(
    compiled_mgc: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, returncode=0, stdout="", stderr=""),  # noqa: ARG005
    )
    exit_code = main(
        ["rpm", "--mgc", str(compiled_mgc), "--name", "x", "--version", "1.0"],
    )
    assert exit_code == 1
