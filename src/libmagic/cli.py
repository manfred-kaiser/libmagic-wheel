"""CLI entry point: compile a .mgc, classify files with it, or diff overrides.

compile: every file placed in --override-dir is applied 1:1:
  - its name matches a bundled Magdir fragment -> replaces that fragment
  - its name matches nothing bundled              -> added as a new fragment
  Pass --rpm to also wrap the freshly compiled .mgc in an RPM, in the
  same step -- entirely optional, the compile itself never requires
  rpmbuild. By default the RPM installs the database as
  /etc/libmagic-wheel/<name>.mgc, so differently-named databases don't
  collide with each other on the same system, and the RPM *package*
  name matches --name too -- both can be overridden independently
  (--install-path, --rpm-name), e.g. to match an existing internal
  naming scheme without changing where the file actually ends up, or
  vice versa. The RPM's version
  defaults to the current UTC timestamp (there's no meaningful "next
  version" to derive otherwise -- a database rebuilt from unchanged
  inputs is still a new build); name, version, release, license, and
  install path are all validated against strict allowlists before ever
  touching the generated spec file -- rpmbuild's %install and %files
  sections are executed as real shell script by rpmbuild itself, so an
  unvalidated value there is a spec/shell injection vector, not just a
  formatting concern.

classify: prints "path: description (mime_type)" per file, like file(1),
but always both fields together rather than picking one via a flag.

diff: side-by-side (`diff -y`) comparison of each override file against
the bundled Magdir fragment it would replace, so you can review what a
compile would actually change before running it. Uses the system
`diff` binary -- no extra dependency just for this.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from ._core import (
    Magic,
    MagicError,
    bundled_magdir,
    compile_database,
)

# RFC-ish allowlists, deliberately stricter than what RPM itself accepts --
# the goal is "obviously cannot break out of the spec file or a shell
# scriptlet", not "matches every technically legal RPM name".
_RPM_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_RPM_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+~]{0,63}$")
_RPM_FREE_TEXT_RE = re.compile(r"^[A-Za-z0-9 ._+()-]{1,128}$")
_INSTALL_PATH_RE = re.compile(r"^/[A-Za-z0-9._+/-]{0,255}$")
_SAFE_ABS_PATH_RE = re.compile(r"^/[A-Za-z0-9._+/-]+$")

_RPM_SPEC_TEMPLATE = """\
Name: {name}
Version: {version}
Release: {release}
Summary: {summary}
License: {license}
BuildArch: noarch

%description
{summary}

%install
mkdir -p "%{{buildroot}}$(dirname {install_path})"
install -m 0644 {source_mgc} "%{{buildroot}}{install_path}"

%files
{install_path}
"""


def _default_rpm_version() -> str:
    return datetime.now(UTC).strftime("%Y%m%d%H%M")


def _validate(value: str, pattern: re.Pattern[str], what: str) -> str:
    if not pattern.match(value):
        message = f"invalid {what}: {value!r}"
        raise ValueError(message)
    return value


def _validate_install_path(value: str) -> str:
    """Character-allowlist plus the standard normalize-and-compare check.

    os.path.normpath() collapses ".." / "." / redundant slashes without
    touching the filesystem (the target path doesn't exist locally -- it's
    for the system this RPM will be installed on). Requiring the input to
    already equal its own normalized form rejects any of that in one
    general rule, not just a hand-picked ".." special case.
    """
    value = _validate(value, _INSTALL_PATH_RE, "install_path")
    normalized = os.path.normpath(value)
    if normalized != value:
        message = f"invalid install_path: {value!r} (not normalized; did you mean {normalized!r}?)"
        raise ValueError(message)
    return value


def _split_override_dir(override_dir: Path) -> tuple[dict[str, Path], list[Path]]:
    known = {f.name for f in bundled_magdir().iterdir()}
    overrides: dict[str, Path] = {}
    extra: list[Path] = []
    for entry in sorted(override_dir.iterdir()):
        if not entry.is_file():
            continue
        if entry.name in known:
            overrides[entry.name] = entry
        else:
            extra.append(entry)
    return overrides, extra


def build_rpm(  # noqa: PLR0913 - each is an independent, user-meaningful RPM field
    mgc: Path,
    name: str,
    *,
    version: str | None = None,
    release: str = "1",
    license_: str = "Proprietary",
    install_path: str = "/etc/libmagic-wheel/combined.mgc",
    output_dir: Path | None = None,
) -> Path:
    """Wrap a compiled .mgc in a minimal RPM. Raises ValueError/OSError on failure."""
    rpmbuild = shutil.which("rpmbuild")
    if rpmbuild is None:
        message = "rpmbuild not found on PATH -- install the rpm-build package"
        raise OSError(message)
    if not mgc.is_file():
        message = f"not found: {mgc}"
        raise ValueError(message)

    name = _validate(name, _RPM_NAME_RE, "name")
    version = _validate(version or _default_rpm_version(), _RPM_VERSION_RE, "version")
    release = _validate(release, _RPM_VERSION_RE, "release")
    license_ = _validate(license_, _RPM_FREE_TEXT_RE, "license")
    install_path = _validate_install_path(install_path)

    # Resolved by Python from an already-verified existing file, not raw
    # user text -- still allowlist-checked before it reaches the spec,
    # since everything in %install is interpolated into a real shell
    # scriptlet regardless of where the value originated.
    source_mgc = str(mgc.resolve())
    if not _SAFE_ABS_PATH_RE.match(source_mgc):
        message = f"resolves to a path with unsafe characters: {source_mgc}"
        raise ValueError(message)

    with tempfile.TemporaryDirectory() as topdir:
        topdir_path = Path(topdir)
        for sub in ("BUILD", "RPMS", "SOURCES", "SPECS", "SRPMS", "BUILDROOT"):
            (topdir_path / sub).mkdir()

        spec_path = topdir_path / "SPECS" / f"{name}.spec"
        spec_path.write_text(
            _RPM_SPEC_TEMPLATE.format(
                name=name,
                version=version,
                release=release,
                summary=f"{name} magic database",
                license=license_,
                install_path=install_path,
                source_mgc=source_mgc,
            ),
        )

        result = subprocess.run(  # noqa: S603 - resolved executable, fixed argv, no shell, every value allowlist-validated above
            [rpmbuild, "-bb", "--define", f"_topdir {topdir_path}", str(spec_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            message = f"rpmbuild failed:\n{result.stdout}\n{result.stderr}"
            raise OSError(message)

        built = sorted((topdir_path / "RPMS").rglob("*.rpm"))
        if not built:
            message = "rpmbuild reported success but produced no .rpm"
            raise OSError(message)

        dest_dir = output_dir or Path.cwd()
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / built[0].name
        shutil.copy(built[0], dest)
        return dest


def _run_compile(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    overrides: dict[str, Path] = {}
    extra: list[Path] = []
    if args.override_dir is not None:
        if not args.override_dir.is_dir():
            parser.error(f"--override-dir not found: {args.override_dir}")
        overrides, extra = _split_override_dir(args.override_dir)

    dest = compile_database(
        overrides=overrides,
        extra=extra,
        output_dir=args.output_dir,
        output_name=args.name,
    )
    print(f"wrote {dest}")
    if overrides:
        print(f"replaced {len(overrides)} bundled fragment(s): {sorted(overrides)}")
    if extra:
        print(f"added {len(extra)} new fragment(s): {sorted(f.name for f in extra)}")

    if args.rpm:
        rpm_name = args.rpm_name if args.rpm_name is not None else args.name
        install_path = (
            args.install_path
            if args.install_path is not None
            else f"/etc/libmagic-wheel/{args.name}.mgc"
        )
        try:
            rpm_path = build_rpm(
                dest,
                rpm_name,
                install_path=install_path,
                output_dir=args.output_dir,
            )
        except (ValueError, OSError) as exc:
            print(f"RPM build failed: {exc}", file=sys.stderr)
            return 1
        print(f"wrote {rpm_path}")

    return 0


def _run_diff(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if not args.override_dir.is_dir():
        parser.error(f"--override-dir not found: {args.override_dir}")

    diff_bin = shutil.which("diff")
    if diff_bin is None:
        print("diff not found on PATH", file=sys.stderr)
        return 1

    overrides, extra = _split_override_dir(args.override_dir)
    for extra_file in extra:
        print(
            f"{extra_file.name}: new fragment, no bundled original to compare against"
        )

    exit_code = 0
    for fragment_name, override in sorted(overrides.items()):
        bundled = bundled_magdir() / fragment_name
        print(f"=== {fragment_name} ===")
        result = subprocess.run(  # noqa: S603 - resolved executable, fixed argv, no shell
            [diff_bin, "-y", str(bundled), str(override)],
            check=False,
        )
        # exit 1 just means "they differ", the normal/expected case here --
        # only >1 (e.g. a file it couldn't read) is a real diff failure.
        if result.returncode > 1:
            exit_code = 1
    return exit_code


def _run_classify(args: argparse.Namespace) -> int:
    magic = Magic(
        magic_file=str(args.mgc) if args.mgc is not None else None,
        uncompress=args.uncompress,
        allow_compress_fork=args.allow_compress_fork,
    )
    exit_code = 0
    for path in args.paths:
        try:
            result = magic.from_file(str(path))
        except MagicError as exc:
            print(f"{path}: ERROR: {exc}", file=sys.stderr)
            exit_code = 1
            continue
        print(f"{path}: {result.description} ({result.mime_type})")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `libmagic-wheel` console script."""
    parser = argparse.ArgumentParser(prog="libmagic-wheel")
    sub = parser.add_subparsers(dest="command", required=True)

    compile_cmd = sub.add_parser("compile", help="compile a .mgc database")
    compile_cmd.add_argument(
        "--override-dir",
        type=Path,
        default=None,
        help="folder of magic source files; names matching a bundled "
        "Magdir fragment replace it 1:1, other names are added as new "
        "fragments",
    )
    compile_cmd.add_argument(
        "--output-dir",
        type=Path,
        default=Path.cwd(),
        help="where to write <name>.mgc",
    )
    compile_cmd.add_argument("--name", default="combined", help="output file base name")
    compile_cmd.add_argument(
        "--rpm",
        action="store_true",
        help="also wrap the compiled .mgc in an RPM, installed as "
        "/etc/libmagic-wheel/<name>.mgc (optional; everything else about "
        "the RPM uses build_rpm()'s defaults)",
    )
    compile_cmd.add_argument(
        "--rpm-name",
        default=None,
        help="RPM package name, if it should differ from --name "
        "(default: same as --name)",
    )
    compile_cmd.add_argument(
        "--install-path",
        default=None,
        help="absolute path the .mgc is installed to on the target system "
        "(default: /etc/libmagic-wheel/<name>.mgc)",
    )

    classify_cmd = sub.add_parser("classify", help="classify files, file(1)-like")
    classify_cmd.add_argument(
        "--mgc",
        type=Path,
        default=None,
        help="compiled .mgc database (default: the bundled unmodified-Magdir "
        "one, for development/debugging only -- see the README)",
    )
    classify_cmd.add_argument(
        "--uncompress",
        action="store_true",
        help="look inside compressed files (see README)",
    )
    classify_cmd.add_argument(
        "--allow-compress-fork",
        action="store_true",
        help="actually let --uncompress decompress (forks/execs internally) "
        "instead of safely failing every time -- only if this process "
        "already has its own OS-level sandboxing (see README)",
    )
    classify_cmd.add_argument("paths", nargs="+", type=Path, help="files to classify")

    diff_cmd = sub.add_parser(
        "diff",
        help="side-by-side diff of override files against the bundled originals",
    )
    diff_cmd.add_argument(
        "--override-dir",
        type=Path,
        required=True,
        help="folder of magic source files, as passed to compile --override-dir",
    )

    args = parser.parse_args(argv)

    if args.command == "compile":
        return _run_compile(parser, args)
    if args.command == "classify":
        return _run_classify(args)
    if args.command == "diff":
        return _run_diff(parser, args)

    return 1  # pragma: no cover - argparse enforces a valid command


if __name__ == "__main__":
    sys.exit(main())
