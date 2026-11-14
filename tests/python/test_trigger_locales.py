"""The strings of the external trigger exist in all seven languages with the same placeholders (no display needed)."""

from __future__ import annotations

import json
import string

import pytest
from mourse_decoder import backup, trigger
from mourse_decoder.i18n import BUILTIN_LOCALE_DIR

LANGUAGES = ("en", "de", "de-saar", "es", "fr", "it", "pt")
KEYS = {
    "settings.section.trigger": set(),
    "settings.trigger": set(),
    "settings.trigger.hint": set(),
    "settings.trigger.log.title": set(),
    "settings.trigger.log.empty": set(),
    "settings.trigger.status.running": set(),
    "settings.trigger.status.unavailable": set(),
    "functionkeys.triggerable": set(),
    "functionkeys.triggerable.hint": set(),
    "functionkeys.triggerable.sensitive": set(),
    "functionkeys.triggerable.marker": set(),
    "backup.warn.trigger_off": set(),
    "backup.risk.external_trigger": set(),
    "trigger.toast.accepted": {"slot", "time"},
    "trigger.toast.rejected": set(),
    "trigger.log.line": {"time", "slot", "result"},
    **{f"trigger.result.{code}": set() for code in trigger.CODES},
}


def strings(code: str) -> dict[str, str]:
    return json.loads((BUILTIN_LOCALE_DIR / f"{code}.json").read_text(encoding="utf-8"))["strings"]


@pytest.mark.parametrize("code", LANGUAGES)
def test_every_trigger_key_exists_with_its_placeholders(code):
    texts = strings(code)
    for key, names in KEYS.items():
        assert key in texts and texts[key].strip(), f"{code}: {key}"
        found = {name for _, name, _, _ in string.Formatter().parse(texts[key]) if name}
        assert found == names, f"{code}: placeholders of {key}"


def test_the_external_trigger_risk_label_is_what_the_backup_module_emits():
    assert backup.EXTERNAL_TRIGGER_RISK == "external trigger"
    assert strings("en")["backup.risk.external_trigger"] == backup.EXTERNAL_TRIGGER_RISK
