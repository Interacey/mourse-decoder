"""Form parsing for the settings page, kept free of tkinter so tests can cover it."""

from __future__ import annotations

from ..settings import Settings

NUMERIC_FIELDS: dict[str, str] = {
    "dot_threshold_ms": "Punkt/Strich-Schwelle",
    "letter_gap_ms": "Buchstabenpause",
    "word_gap_ms": "Wortpause",
}


def parse_form(values: dict[str, str], base: Settings) -> tuple[Settings | None, list[str]]:
    """Turn form values into Settings: (settings, []) on success, else (None, errors).

    Fields that aren't passed keep their value from ``base``.
    """
    errors: list[str] = []
    changes: dict[str, object] = {}

    for key, label in NUMERIC_FIELDS.items():
        if key not in values:
            continue
        raw = values[key].strip()
        try:
            changes[key] = int(raw)
        except ValueError:
            errors.append(f"{label}: '{raw}' ist keine ganze Zahl.")

    for key in ("trigger", "alphabet"):
        if key in values:
            changes[key] = values[key].strip()

    if errors:
        return None, errors

    candidate = base.replace(**changes)
    errors = candidate.validate()
    return (None, errors) if errors else (candidate, [])
