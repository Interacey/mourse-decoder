"""Ed25519 signatures (RFC 8032) with the standard library only, and the check of a signed release.

The exe is built with PyInstaller and kept small on purpose, so there is no cryptography package here.
Verifying takes a few milliseconds. Signing is not constant-time; it runs only on the maintainer's machine
(packaging/sign_release.py), never in the app.
"""

from __future__ import annotations

import base64
import hashlib
import secrets

from .i18n import tr
from .updater import UpdateError

# Raw 32-byte public keys. An update is accepted if its signature verifies with any key in this tuple, so a new
# key is introduced by shipping it here in an app release that is signed with the old key. The tuple holds the
# current release key.
PINNED_PUBLIC_KEYS: tuple[bytes, ...] = (
    bytes.fromhex("a15cc74b92b932d271e8550957d185606431e1d7401d7b2c5f88e94e004c6466"),
)

_MANIFEST_HEADER = b"mourse-decoder-release-v1\n"

_P = 2**255 - 19
_Q = 2**252 + 27742317777372353535851937790883648493  # order of the base point
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)

# points are (X, Y, Z, T) with x = X/Z, y = Y/Z, x*y = T/Z
_Point = tuple[int, int, int, int]


def _recover_x(y: int, sign: int) -> int | None:
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P:
        return None
    return _P - x if (x & 1) != sign else x


_BASE_Y = 4 * pow(5, _P - 2, _P) % _P
_BASE_X = _recover_x(_BASE_Y, 0)
assert _BASE_X is not None
_BASE: _Point = (_BASE_X, _BASE_Y, 1, _BASE_X * _BASE_Y % _P)
_ZERO: _Point = (0, 1, 1, 0)


def _add(a: _Point, b: _Point) -> _Point:
    p1 = (a[1] - a[0]) * (b[1] - b[0]) % _P
    p2 = (a[1] + a[0]) * (b[1] + b[0]) % _P
    c = 2 * a[3] * b[3] * _D % _P
    d = 2 * a[2] * b[2] % _P
    e, f, g, h = p2 - p1, d - c, d + c, p2 + p1
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(scalar: int, point: _Point) -> _Point:
    result = _ZERO
    while scalar:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _same(a: _Point, b: _Point) -> bool:
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _encode(point: _Point) -> bytes:
    z_inv = pow(point[2], _P - 2, _P)
    x, y = point[0] * z_inv % _P, point[1] * z_inv % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decode(data: bytes) -> _Point | None:
    if len(data) != 32:
        return None
    y = int.from_bytes(data, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


def _hash_mod_q(data: bytes) -> int:
    return int.from_bytes(hashlib.sha512(data).digest(), "little") % _Q


def _expand(seed: bytes) -> tuple[int, bytes]:
    if len(seed) != 32:
        raise ValueError("an Ed25519 seed is 32 bytes")
    digest = hashlib.sha512(seed).digest()
    return int.from_bytes(digest[:32], "little") & ((1 << 254) - 8) | (1 << 254), digest[32:]


def generate_seed() -> bytes:
    return secrets.token_bytes(32)


def public_key(seed: bytes) -> bytes:
    return _encode(_mul(_expand(seed)[0], _BASE))


def sign(seed: bytes, message: bytes) -> bytes:
    """The 64-byte signature of ``message``."""
    secret, prefix = _expand(seed)
    key = _encode(_mul(secret, _BASE))
    r = _hash_mod_q(prefix + message)
    r_enc = _encode(_mul(r, _BASE))
    s = (r + _hash_mod_q(r_enc + key + message) * secret) % _Q
    return r_enc + s.to_bytes(32, "little")


def verify(public_key: bytes, signature: bytes, message: bytes) -> bool:
    """True if ``signature`` is valid for ``message``; any malformed input gives False, never an exception."""
    if not all(isinstance(x, (bytes, bytearray)) for x in (public_key, signature, message)):
        return False
    public_key, signature, message = bytes(public_key), bytes(signature), bytes(message)
    if len(signature) != 64:
        return False
    key, r = _decode(public_key), _decode(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if key is None or r is None or s >= _Q:
        return False
    if _same(_mul(8, key), _ZERO):  # a key of small order (the identity, say) lets anybody forge a signature
        return False
    h = _hash_mod_q(signature[:32] + public_key + message)
    return _same(_mul(s, _BASE), _add(r, _mul(h, key)))


def build_manifest(tag: str, asset_name: str, sha256_hex: str) -> bytes:
    """The text that is signed. Tag and file name are part of it, so a signature cannot be moved to another
    release (a downgrade) or another file."""
    if "\n" in tag or "\n" in asset_name:
        raise ValueError("tag and asset name must not contain a line break")
    return _MANIFEST_HEADER + f"{tag}\n{asset_name}\n{sha256_hex.lower()}\n".encode()


def encode_signature(sig: bytes) -> str:
    if len(sig) != 64:
        raise ValueError("an Ed25519 signature is 64 bytes")
    return base64.b64encode(sig).decode("ascii") + "\n"


def parse_signature(text: str | bytes) -> bytes:
    """The 64 signature bytes from the text of a .sig file; ValueError for anything else."""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("ascii")  # UnicodeDecodeError is a ValueError
    sig = base64.b64decode(text.strip(), validate=True)  # binascii.Error is a ValueError
    if len(sig) != 64:
        raise ValueError("a signature is 64 bytes")
    return sig


class SignatureError(UpdateError):
    """The release signature is missing a key, malformed or wrong. ``reason`` is ``no_pinned_key``,
    ``bad_format`` or ``bad_signature``."""

    def __init__(self, reason: str) -> None:
        key = "update.err.no_pinned_key" if reason == "no_pinned_key" else "update.err.bad_signature"
        super().__init__(tr(key), open_page=reason == "no_pinned_key")
        self.reason = reason


def verify_release(tag: str, asset_name: str, sha256_hex: str, signature_text: str | bytes) -> None:
    """Return if ``signature_text`` is a valid signature of this tag, file name and hash by a pinned key."""
    if not PINNED_PUBLIC_KEYS:
        raise SignatureError("no_pinned_key")
    try:
        signature = parse_signature(signature_text)
        manifest = build_manifest(tag, asset_name, sha256_hex)
    except ValueError:
        raise SignatureError("bad_format") from None
    if not any(verify(key, signature, manifest) for key in PINNED_PUBLIC_KEYS):
        raise SignatureError("bad_signature")
