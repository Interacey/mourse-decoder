"""The version lives in three files; a release with mismatched numbers shows the wrong one."""

from __future__ import annotations

import re
from pathlib import Path

import mourse_decoder
import pytest
from mourse_decoder._native import NATIVE_AVAILABLE

ROOT = Path(__file__).resolve().parents[2]


def declared_version(file_name: str) -> str:
    text = (ROOT / file_name).read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
    assert match, f"no version in {file_name}"
    return match.group(1)


def test_versions_match_across_python_and_rust():
    assert declared_version("pyproject.toml") == mourse_decoder.__version__
    assert declared_version("Cargo.toml") == mourse_decoder.__version__


@pytest.mark.skipif(not NATIVE_AVAILABLE, reason="Rust-Modul nicht gebaut (maturin develop)")
def test_built_native_module_has_the_same_version():
    from mourse_decoder import _morse_core

    assert _morse_core.__version__ == mourse_decoder.__version__
