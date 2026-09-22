"""Shared fixtures for the libmagic test suite."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from libmagic import compile_database

if TYPE_CHECKING:
    from pathlib import Path

# Minimal, syntactically valid inputs for exercising real classification
# without depending on fixture files elsewhere on disk.
PDF_BYTES = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
TEXT_BYTES = b"just plain ascii text\n"


@pytest.fixture(scope="session")
def compiled_mgc(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A .mgc compiled once from the unmodified bundled Magdir, shared by tests."""
    out_dir = tmp_path_factory.mktemp("mgc")
    return compile_database(output_dir=out_dir)
