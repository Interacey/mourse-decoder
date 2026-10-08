"""Loads the Rust extension and explains what to do when it isn't built."""

from __future__ import annotations

from types import ModuleType

try:
    from . import _morse_core as _module  # type: ignore[attr-defined]
except ImportError as exc:  # pragma: no cover
    _module = None
    _IMPORT_ERROR: ImportError | None = exc
else:
    _IMPORT_ERROR = None

NATIVE_AVAILABLE: bool = _module is not None


def load_native() -> ModuleType:
    if _module is None:
        raise RuntimeError(
            "Das Rust-Modul 'mourse_decoder._morse_core' ist nicht gebaut.\n"
            "Im Projektordner ausführen:  maturin develop --release\n"
            f"(Ursprünglicher Fehler: {_IMPORT_ERROR})"
        )
    return _module
