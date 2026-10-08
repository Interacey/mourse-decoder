"""Mourse Decoder, the Python frontend.

Importing the package must not need a display, pystray or the compiled Rust
module (tests rely on that), so nothing heavy is imported here.
"""

from .decoders import AlphabetDecoder, Emission, TelecodeDecoder, create_decoder
from .dictionary import Alphabet, AlphabetError, AlphabetInfo, DictionaryManager, load_alphabet
from .settings import Settings, SettingsStore

__all__ = [
    "Alphabet",
    "AlphabetDecoder",
    "AlphabetError",
    "AlphabetInfo",
    "DictionaryManager",
    "Emission",
    "Settings",
    "SettingsStore",
    "TelecodeDecoder",
    "create_decoder",
    "load_alphabet",
]

__version__ = "0.1.0"
