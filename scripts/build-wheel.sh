#!/usr/bin/env bash
# Builds the wheel with a correct platform tag (py3-none-linux_x86_64),
# without a setup.py shim -- setuptools' own --plat-name build option,
# passed through PEP 517 config-settings, is enough on its own. See the
# project README for why "any" would be wrong here (the package bundles
# an x86_64 Linux libmagic.so) and why cp3xx-* would be unnecessarily
# narrow (the Python-facing code is pure ctypes, no CPython ABI
# dependency).
#
# Requires scripts/build-lib.sh to have populated src/libmagic/data/ first.
#
# Usage: scripts/build-wheel.sh [output-dir]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${1:-$REPO_ROOT/dist}"

if [ ! -f "$REPO_ROOT/src/libmagic/data/libmagic.so.1" ]; then
    echo "missing $REPO_ROOT/src/libmagic/data/libmagic.so.1 -- run scripts/build-lib.sh first" >&2
    exit 1
fi

python3 -m build --wheel --outdir "$OUT_DIR" "$REPO_ROOT" \
    -C--build-option=--plat-name -C--build-option=linux_x86_64
