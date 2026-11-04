"""A replaced decoder is closed after the new one is installed."""

from __future__ import annotations

from mourse_decoder.controller import MorseController
from mourse_decoder.decoders import Emission
from mourse_decoder.dictionary import DictionaryManager
from mourse_decoder.settings import Settings


class ClosingDecoder:
    def __init__(self, log, name, fail=False):
        self.log, self.name, self.fail = log, name, fail

    def decode(self, sequence):
        return Emission(self.name)

    def word_gap(self):
        return None

    def reset(self):
        pass

    def close(self):
        self.log.append(f"close {self.name}")
        if self.fail:
            raise OSError("boom")


def _controller(fake_engine, injector, log, **kwargs):
    made = iter([ClosingDecoder(log, "old", **kwargs), ClosingDecoder(log, "new")])

    def factory(_d, _a):
        decoder = next(made)
        log.append(f"build {decoder.name}")
        return decoder

    return MorseController(fake_engine, injector, DictionaryManager(), Settings(), decoder_factory=factory)


def test_apply_settings_closes_old_decoder_after_installing_new(fake_engine, injector):
    log: list[str] = []
    controller = _controller(fake_engine, injector, log)
    controller.apply_settings(Settings(alphabet="morse"))
    assert log == ["build old", "build new", "close old"]
    assert controller._decoder.name == "new"


def test_reload_alphabet_closes_old_decoder(fake_engine, injector):
    log: list[str] = []
    controller = _controller(fake_engine, injector, log)
    controller.reload_alphabet()
    assert log == ["build old", "build new", "close old"]


def test_close_failure_does_not_break_the_swap(fake_engine, injector):
    log: list[str] = []
    controller = _controller(fake_engine, injector, log, fail=True)
    controller.reload_alphabet()
    assert controller._decoder.name == "new"


def test_decoder_without_close_is_fine(fake_engine, injector):
    class Bare:
        def decode(self, sequence):
            return None

        def word_gap(self):
            return None

        def reset(self):
            pass

    made = iter([Bare(), Bare()])
    controller = MorseController(
        fake_engine, injector, DictionaryManager(), Settings(), decoder_factory=lambda _d, _a: next(made)
    )
    controller.reload_alphabet()
