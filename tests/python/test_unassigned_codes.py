"""No code that an alphabet does not assign may produce text."""

from __future__ import annotations

import itertools

import pytest
from mourse_decoder.decoders import AlphabetDecoder
from mourse_decoder.dictionary import DictionaryManager
from mourse_decoder.smileys import load_smiley_codes

SEQUENCES = ["".join(p) for n in range(1, 9) for p in itertools.product(".-", repeat=n)]
MANAGER = DictionaryManager()
IDS = [a.id for a in MANAGER.available()]
SETS = ["", *[s.id for s in MANAGER.smiley_sets()]]


@pytest.mark.parametrize("smiley_id", SETS)
@pytest.mark.parametrize("alphabet_id", IDS)
def test_only_assigned_codes_give_text(alphabet_id, smiley_id):
    manager = DictionaryManager()
    manager.set_smileys(smiley_id)
    raw = manager.load(alphabet_id)
    assigned = {**load_smiley_codes(smiley_id), **raw.codes, **raw.combining}
    decoder = AlphabetDecoder(manager.select(alphabet_id))
    for sequence in SEQUENCES:
        decoder.reset()
        emission = decoder.decode(sequence)
        if sequence in assigned:
            assert emission is not None or sequence in raw.combining, sequence
        else:
            assert emission is None, f"{alphabet_id}/{smiley_id}: {sequence} typed {emission.text!r}"


def test_seven_is_only_on_its_own_code():
    decoder = AlphabetDecoder(MANAGER.select("german"))
    typed = {s: decoder.decode(s) for s in SEQUENCES}
    assert [s for s, e in typed.items() if e and e.text == "7"] == ["--..."]
    assert not any(e and "sieben" in e.text.lower() for e in typed.values())


def test_overlong_sequence_is_unknown():
    decoder = AlphabetDecoder(MANAGER.select("latin"))
    assert decoder.decode("-" * 33) is None
