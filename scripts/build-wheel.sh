#!/usr/bin/env bash
# Builds the wheel and repairs it into a real manylinux wheel, without a
# setup.py shim -- setuptools' own --plat-name build option, passed
# through PEP 517 config-settings, produces an intermediate
# py3-none-linux_x86_64 wheel (cp3xx-* would be unnecessarily narrow --
# the Python-facing code is pure ctypes, no CPython ABI dependency), but
# PyPI rejects that tag outright ("unsupported platform tag
# 'linux_x86_64'") -- it only accepts manylinux/musllinux tags, which
# assert a specific glibc baseline and that every non-baseline shared
# library the wheel depends on (e.g. liblzma, dynamically linked by
# libmagic.so itself) is vendored into the wheel, not just assumed
# present on whatever system pip install runs on. `auditwheel repair`
# determines the correct tag from the actual ELF symbol versions used
# and does that vendoring for us.
#
# Requires scripts/build-lib.sh to have populated src/libmagic/data/ first.
#
# Usage: scripts/build-wheel.sh [output-dir]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${1:-$REPO_ROOT/dist}"
RAW_DIR="$(mktemp -d)"
trap 'rm -rf "$RAW_DIR"' EXIT

if [ ! -f "$REPO_ROOT/src/libmagic/data/libmagic.so.1" ]; then
    echo "missing $REPO_ROOT/src/libmagic/data/libmagic.so.1 -- run scripts/build-lib.sh first" >&2
    exit 1
fi

python3 -m build --wheel --outdir "$RAW_DIR" "$REPO_ROOT" \
    -C--build-option=--plat-name -C--build-option=linux_x86_64

mkdir -p "$OUT_DIR"
python3 -m auditwheel repair "$RAW_DIR"/*.whl -w "$OUT_DIR"
