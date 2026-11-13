"""No code that an alphabet does not assign may produce text."""

from __future__ import annotations

import itertools
import json
import time

import pytest
from mourse_decoder._native import NATIVE_AVAILABLE, load_native
from mourse_decoder.controller import MorseController
from mourse_decoder.decoders import AlphabetDecoder, TelecodeDecoder, create_decoder
from mourse_decoder.dictionary import DictionaryManager
from mourse_decoder.settings import Settings
from mourse_decoder.smileys import load_smiley_codes
from mourse_decoder.telecode import TelecodeStore

SHORT = ["".join(p) for n in range(1, 9) for p in itertools.product(".-", repeat=n)]
MANAGER = DictionaryManager()
IDS = [a.id for a in MANAGER.available()]
SETS = ["", *[s.id for s in MANAGER.smiley_sets()]]


def long_sequences(assigned):
    """9 to 32 symbols: uniform and alternating runs, and every assigned code with one symbol added or cut."""
    found = set()
    for n in range(9, 33):
        found.update({"." * n, "-" * n, ("-." * n)[:n], (".-" * n)[:n]})
    for code in assigned:
        for symbol in ".-":
            found.add((code + symbol)[:32])
            found.add((symbol + code)[:32])
        found.add(code[1:] or "-")
    return sorted(found)


def check(manager, alphabet_id, smiley_id):
    manager.set_smileys(smiley_id)
    raw = manager.load(alphabet_id)
    assigned = {**load_smiley_codes(smiley_id, manager.smiley_directory, manager.user_smiley_directory),
                **raw.codes, **raw.combining}
    decoder = AlphabetDecoder(manager.select(alphabet_id))
    for sequence in [*SHORT, *long_sequences(assigned)]:
        decoder.reset()
        emission = decoder.decode(sequence)
        if sequence in assigned:
            assert emission is not None or sequence in raw.combining, sequence
        else:
            assert emission is None, f"{alphabet_id}/{smiley_id}: {sequence} typed {emission.text!r}"


@pytest.mark.parametrize("smiley_id", SETS)
@pytest.mark.parametrize("alphabet_id", IDS)
def test_only_assigned_codes_give_text(alphabet_id, smiley_id):
    check(DictionaryManager(), alphabet_id, smiley_id)


def test_user_alphabet_and_user_smileys_with_extends(tmp_path):
    alphabets, smileys = tmp_path / "alphabets", tmp_path / "smileys"
    alphabets.mkdir()
    smileys.mkdir()
    (alphabets / "mine.json").write_text(json.dumps(
        {"id": "mine", "extends": "german", "codes": {"-" * 32: "long", ".-.-.-.-": "x"}}), encoding="utf-8")
    (alphabets / "plain.json").write_text(json.dumps(
        {"id": "plain", "extends": "common", "codes": {"..--.-": "y"}}), encoding="utf-8")
    (smileys / "mine.json").write_text(json.dumps(
        {"id": "mine", "codes": {"--..-.-.": "Hello{Enter}", "...-...-": "z"}}), encoding="utf-8")
    manager = DictionaryManager(user_directory=alphabets, user_smiley_directory=smileys)
    for alphabet_id in ("mine", "plain"):
        for smiley_id in ("", "mine", "standard"):
            check(manager, alphabet_id, smiley_id)


def test_seven_is_only_on_its_own_code():
    decoder = AlphabetDecoder(MANAGER.select("german"))
    typed = {s: decoder.decode(s) for s in SHORT}
    assert [s for s, e in typed.items() if e and e.text == "7"] == ["--..."]
    assert not any(e and "sieben" in e.text.lower() for e in typed.values())


def test_telecode_unknown_four_digit_code_types_its_digits(telecode_db):
    """Documented current behaviour, not a decision: in the telecode mode a four-digit code that is not in the
    database leaves the digits in the text (decoders.py, TelecodeDecoder.decode) so the user sees them.
    Everything that is not a digit still types nothing."""
    manager = DictionaryManager(telecode_db=telecode_db)
    decoder = create_decoder(manager, DictionaryManager.TELECODE_ID)
    assert isinstance(decoder, TelecodeDecoder)
    try:
        digits = {c: ch for c, ch in manager.load("common").codes.items() if ch.isdigit()}
        by_digit = {ch: c for c, ch in digits.items()}
        typed = [decoder.decode(by_digit[d]) for d in "1234"]
        assert [e.text for e in typed] == ["1", "2", "3", "4"]  # 1234 is not in the sample database
        for sequence in SHORT:
            decoder.reset()
            emission = decoder.decode(sequence)
            assert (emission is not None) == (sequence in digits), sequence
    finally:
        decoder.close()


def test_telecode_store_is_not_a_fallback_for_alphabets(telecode_db):
    with TelecodeStore(telecode_db) as store:
        assert store.lookup("0022") == "中"
        assert store.lookup(".-") is None


class Injector:
    def __init__(self):
        self.out = []

    def inject_text(self, text):
        self.out.append(text)

    type_text = inject_text

    def pastes(self, text):
        return False

    def inject_backspaces(self, count):
        pass

    def press_shortcut(self, combo):
        pass

    def check_shortcut(self, combo):
        pass

    def foreground_title(self):
        return ""

    def clipboard_text(self):
        return None

    def paste_files(self, paths):
        pass

    def scroll(self, notches):
        pass


@pytest.mark.skipif(not NATIVE_AVAILABLE, reason="Rust module not built")
def test_engine_overflow_is_unknown_and_32_symbols_still_match(tmp_path):
    """40 keyed symbols must not come out as the 32-symbol code; exactly 32 still must."""
    alphabets = tmp_path / "alphabets"
    alphabets.mkdir()
    (alphabets / "long.json").write_text(json.dumps(
        {"id": "long", "extends": "latin", "codes": {"-" * 32: "LONG"}}), encoding="utf-8")
    manager = DictionaryManager(user_directory=alphabets)
    native = load_native()
    engine = native.MorseEngine(trigger="ScrollLock")
    injector = Injector()
    seen = []

    class Listener:
        def on_symbol(self, symbol):
            pass

        def on_letter(self, sequence, text):
            seen.append((len(sequence), text))

        def on_error(self, message):
            raise AssertionError(message)

    settings = Settings(alphabet="long", letter_gap_ms=100, word_gap_ms=0, typing_delay_ms=0)
    controller = MorseController(engine, injector, manager, settings, listener=Listener(), use_hook=False)
    controller.start()
    try:
        for count in (40, 32):
            for _ in range(count):
                engine.feed_press()
                engine.feed_release("-")
            time.sleep(0.4)
    finally:
        controller.stop()
    assert seen == [(33, None), (32, "LONG")]
    assert injector.out == ["LONG"]
