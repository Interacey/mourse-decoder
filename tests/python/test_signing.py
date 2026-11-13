"""Ed25519 in the standard library (RFC 8032 test vectors) and the check of a signed release."""

from __future__ import annotations

import base64
import os
import time

import pytest
from mourse_decoder import signing
from mourse_decoder.i18n import set_language
from mourse_decoder.signing import SignatureError
from mourse_decoder.updater import UpdateError


@pytest.fixture(autouse=True)
def english():
    set_language("en")
    yield
    set_language("en")


# RFC 8032, section 7.1: (secret key, public key, message, signature), all hex
RFC_8032_VECTORS = [
    # TEST 1
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        (
            "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
            "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
        ),
    ),
    # TEST 2
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        (
            "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
            "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"
        ),
    ),
    # TEST 3
    (
        "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "af82",
        (
            "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
            "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"
        ),
    ),
    # TEST 1024
    (
        "f5e5767cf153319517630f226876b86c8160cc583bc013744c6bf255f5cc0ee5",
        "278117fc144c72340f67d0f2316e8386ceffbf2b2428c9c51fef7c597f1d426e",
        (
            "08b8b2b733424243760fe426a4b54908632110a66c2f6591eabd3345e3e4eb98"
            "fa6e264bf09efe12ee50f8f54e9f77b1e355f6c50544e23fb1433ddf73be84d8"
            "79de7c0046dc4996d9e773f4bc9efe5738829adb26c81b37c93a1b270b20329d"
            "658675fc6ea534e0810a4432826bf58c941efb65d57a338bbd2e26640f89ffbc"
            "1a858efcb8550ee3a5e1998bd177e93a7363c344fe6b199ee5d02e82d522c4fe"
            "ba15452f80288a821a579116ec6dad2b3b310da903401aa62100ab5d1a36553e"
            "06203b33890cc9b832f79ef80560ccb9a39ce767967ed628c6ad573cb116dbef"
            "efd75499da96bd68a8a97b928a8bbc103b6621fcde2beca1231d206be6cd9ec7"
            "aff6f6c94fcd7204ed3455c68c83f4a41da4af2b74ef5c53f1d8ac70bdcb7ed1"
            "85ce81bd84359d44254d95629e9855a94a7c1958d1f8ada5d0532ed8a5aa3fb2"
            "d17ba70eb6248e594e1a2297acbbb39d502f1a8c6eb6f1ce22b3de1a1f40cc24"
            "554119a831a9aad6079cad88425de6bde1a9187ebb6092cf67bf2b13fd65f270"
            "88d78b7e883c8759d2c4f5c65adb7553878ad575f9fad878e80a0c9ba63bcbcc"
            "2732e69485bbc9c90bfbd62481d9089beccf80cfe2df16a2cf65bd92dd597b07"
            "07e0917af48bbb75fed413d238f5555a7a569d80c3414a8d0859dc65a46128ba"
            "b27af87a71314f318c782b23ebfe808b82b0ce26401d2e22f04d83d1255dc51a"
            "ddd3b75a2b1ae0784504df543af8969be3ea7082ff7fc9888c144da2af58429e"
            "c96031dbcad3dad9af0dcbaaaf268cb8fcffead94f3c7ca495e056a9b47acdb7"
            "51fb73e666c6c655ade8297297d07ad1ba5e43f1bca32301651339e22904cc8c"
            "42f58c30c04aafdb038dda0847dd988dcda6f3bfd15c4b4c4525004aa06eeff8"
            "ca61783aacec57fb3d1f92b0fe2fd1a85f6724517b65e614ad6808d6f6ee34df"
            "f7310fdc82aebfd904b01e1dc54b2927094b2db68d6f903b68401adebf5a7e08"
            "d78ff4ef5d63653a65040cf9bfd4aca7984a74d37145986780fc0b16ac451649"
            "de6188a7dbdf191f64b5fc5e2ab47b57f7f7276cd419c17a3ca8e1b939ae49e4"
            "88acba6b965610b5480109c8b17b80e1b7b750dfc7598d5d5011fd2dcc5600a3"
            "2ef5b52a1ecc820e308aa342721aac0943bf6686b64b2579376504ccc493d97e"
            "6aed3fb0f9cd71a43dd497f01f17c0e2cb3797aa2a2f256656168e6c496afc5f"
            "b93246f6b1116398a346f1a641f3b041e989f7914f90cc2c7fff357876e506b5"
            "0d334ba77c225bc307ba537152f3f1610e4eafe595f6d9d90d11faa933a15ef1"
            "369546868a7f3a45a96768d40fd9d03412c091c6315cf4fde7cb68606937380d"
            "b2eaaa707b4c4185c32eddcdd306705e4dc1ffc872eeee475a64dfac86aba41c"
            "0618983f8741c5ef68d3a101e8a3b8cac60c905c15fc910840b94c00a0b9d0"
        ),
        (
            "0aab4c900501b3e24d7cdf4663326a3a87df5e4843b2cbdb67cbf6e460fec350"
            "aa5371b1508f9f4528ecea23c436d94b5e8fcd4f681e30a6ac00a9704a188a03"
        ),
    ),
]


def hexes(vector):
    return tuple(bytes.fromhex(part) for part in vector)


# --- Ed25519 ---


@pytest.mark.parametrize("vector", RFC_8032_VECTORS, ids=["test 1", "test 2", "test 3", "test 1024"])
def test_rfc_8032_vectors(vector):
    seed, public, message, sig = hexes(vector)
    assert signing.public_key(seed) == public
    assert signing.sign(seed, message) == sig
    assert signing.verify(public, sig, message)


def test_signing_and_verifying_with_random_keys_and_lengths():
    for length in (0, 1, 2, 31, 32, 33, 63, 64, 65, 200):
        seed, message = signing.generate_seed(), os.urandom(length)
        public = signing.public_key(seed)
        sig = signing.sign(seed, message)
        assert len(public) == 32 and len(sig) == 64
        assert signing.verify(public, sig, message)
        assert signing.sign(seed, message) == sig  # no random part


def test_generate_seed_is_32_fresh_bytes():
    seeds = {signing.generate_seed() for _ in range(5)}
    assert len(seeds) == 5 and all(len(s) == 32 for s in seeds)


@pytest.mark.parametrize("size", [0, 31, 33, 64])
def test_a_seed_of_the_wrong_length_is_refused(size):
    with pytest.raises(ValueError):
        signing.public_key(bytes(size))
    with pytest.raises(ValueError):
        signing.sign(bytes(size), b"x")


SEED = bytes.fromhex(RFC_8032_VECTORS[1][0])
PUBLIC = signing.public_key(SEED)
MESSAGE = b"mourse"
SIG = signing.sign(SEED, MESSAGE)


@pytest.mark.parametrize("bit", range(0, 512, 37))
def test_a_flipped_bit_in_the_signature_is_refused(bit):
    bad = bytearray(SIG)
    bad[bit // 8] ^= 1 << (bit % 8)
    assert not signing.verify(PUBLIC, bytes(bad), MESSAGE)


@pytest.mark.parametrize("bit", range(0, 48, 7))
def test_a_flipped_bit_in_the_message_is_refused(bit):
    bad = bytearray(MESSAGE)
    bad[bit // 8] ^= 1 << (bit % 8)
    assert not signing.verify(PUBLIC, SIG, bytes(bad))
    assert not signing.verify(PUBLIC, SIG, MESSAGE + b"\0") and not signing.verify(PUBLIC, SIG, b"")


@pytest.mark.parametrize("bit", range(0, 256, 29))
def test_a_flipped_bit_in_the_key_is_refused(bit):
    bad = bytearray(PUBLIC)
    bad[bit // 8] ^= 1 << (bit % 8)
    assert not signing.verify(bytes(bad), SIG, MESSAGE)


def test_another_key_is_refused():
    assert not signing.verify(signing.public_key(signing.generate_seed()), SIG, MESSAGE)


def test_s_that_is_not_below_the_group_order_is_refused():
    # S + L passes the equation too, but a second valid encoding of one signature would be malleable
    s = int.from_bytes(SIG[32:], "little")
    for bigger in (s + signing._Q, signing._Q):
        assert not signing.verify(PUBLIC, SIG[:32] + bigger.to_bytes(32, "little"), MESSAGE)
    assert signing.verify(PUBLIC, SIG[:32] + s.to_bytes(32, "little"), MESSAGE)


@pytest.mark.parametrize("size", [0, 1, 32, 63, 65, 128])
def test_a_signature_of_the_wrong_length_is_refused(size):
    assert not signing.verify(PUBLIC, bytes(size), MESSAGE)
    assert not signing.verify(PUBLIC, (SIG * 2)[:size], MESSAGE)


@pytest.mark.parametrize("size", [0, 1, 31, 33, 64])
def test_a_key_of_the_wrong_length_is_refused(size):
    assert not signing.verify((PUBLIC * 2)[:size], SIG, MESSAGE)


@pytest.mark.parametrize("junk", [None, "text", 5, [1, 2]])
def test_verify_never_raises_on_malformed_input(junk):
    assert not signing.verify(junk, SIG, MESSAGE)
    assert not signing.verify(PUBLIC, junk, MESSAGE)
    assert not signing.verify(PUBLIC, SIG, junk)


def test_verify_takes_bytearrays():
    assert signing.verify(bytearray(PUBLIC), bytearray(SIG), bytearray(MESSAGE))


def test_points_that_are_not_on_the_curve_are_refused():
    off_curve = next(y for y in range(2, 100) if signing._decode(y.to_bytes(32, "little")) is None)
    key = off_curve.to_bytes(32, "little")
    assert not signing.verify(key, SIG, MESSAGE)
    assert not signing.verify(PUBLIC, key + SIG[32:], MESSAGE)  # R off the curve
    non_canonical = (2**255 - 19).to_bytes(32, "little")  # y = p, a second spelling of y = 0
    assert not signing.verify(non_canonical, SIG, MESSAGE)
    assert not signing.verify(PUBLIC, non_canonical + SIG[32:], MESSAGE)
    assert not signing.verify(bytes(32), bytes(64), MESSAGE) and not signing.verify(b"\xff" * 32, b"\xff" * 64, MESSAGE)


def small_order_encodings() -> list[bytes]:
    """The 8 points whose order divides 8, as public keys: the multiples of one point of order 8."""
    for y in range(2, 100):
        point = signing._decode(y.to_bytes(32, "little"))
        if point is None:
            continue
        torsion = signing._mul(signing._Q, point)  # the part of the point outside the prime-order subgroup
        if not signing._same(signing._mul(4, torsion), signing._ZERO):
            return [signing._encode(signing._mul(k, torsion)) for k in range(8)]
    raise AssertionError("no point of order 8 found")


SMALL_ORDER_KEYS = small_order_encodings()
IDENTITY = bytes([1]) + bytes(31)  # (0, 1)
ORDER_TWO = (signing._P - 1).to_bytes(32, "little")  # (0, -1)


def test_the_small_order_encodings_are_the_expected_eight():
    assert len(set(SMALL_ORDER_KEYS)) == 8
    assert IDENTITY in SMALL_ORDER_KEYS and ORDER_TWO in SMALL_ORDER_KEYS
    for key in SMALL_ORDER_KEYS:
        assert signing._same(signing._mul(8, signing._decode(key)), signing._ZERO)


def test_the_identity_as_public_key_cannot_forge_a_signature():
    # R = identity and S = 0 satisfy S*B == R + h*A for A = identity, whatever the message is
    forged = IDENTITY + bytes(32)
    for message in (b"", b"mourse", os.urandom(40)):
        assert not signing.verify(IDENTITY, forged, message)


@pytest.mark.parametrize("index", range(8))
def test_a_public_key_of_small_order_is_refused(index):
    key = SMALL_ORDER_KEYS[index]
    forged = IDENTITY + bytes(32)
    # for a key of order 2, 4 or 8 the forgery is valid for the messages whose hash is a multiple of that order
    assert not any(signing.verify(key, forged, bytes([i])) for i in range(64))
    assert not signing.verify(key, SIG, MESSAGE)


def test_a_small_order_key_does_not_pass_the_release_check(monkeypatch):
    monkeypatch.setattr(signing, "PINNED_PUBLIC_KEYS", (IDENTITY,))
    forged = base64.b64encode(IDENTITY + bytes(32)).decode()
    assert reason(TAG, ASSET, SHA, forged) == "bad_signature"


def test_one_verification_is_fast():
    start = time.perf_counter()
    assert signing.verify(PUBLIC, SIG, MESSAGE)
    assert time.perf_counter() - start < 1.0  # it takes a few milliseconds; the update must not feel it


# --- the signed text and the .sig file ---


def test_the_manifest_has_exactly_this_form():
    assert signing.build_manifest("v.0.1.9", "MourseDecoder.exe", "AB" * 32) == (
        b"mourse-decoder-release-v1\nv.0.1.9\nMourseDecoder.exe\n" + b"ab" * 32 + b"\n"
    )


@pytest.mark.parametrize("args", [("v2", "A.exe", "00"), ("v1", "B.exe", "00"), ("v1", "A.exe", "01")])
def test_every_part_of_the_manifest_changes_the_text(args):
    assert signing.build_manifest(*args) != signing.build_manifest("v1", "A.exe", "00")


@pytest.mark.parametrize("args", [("v1\nx", "A.exe", "00"), ("v1", "A.exe\nx", "00")])
def test_a_line_break_in_tag_or_name_is_refused(args):
    with pytest.raises(ValueError):
        signing.build_manifest(*args)


def test_the_signature_file_is_one_line_of_base64():
    text = signing.encode_signature(SIG)
    assert text.endswith("\n") and text.count("\n") == 1 and base64.b64decode(text) == SIG
    with pytest.raises(ValueError):
        signing.encode_signature(SIG[:-1])


@pytest.mark.parametrize("wrap", [lambda t: t, lambda t: t.encode(), lambda t: f"  {t.strip()} \r\n\r\n", lambda t: "\n" + t])
def test_parse_signature_reads_what_encode_wrote(wrap):
    assert signing.parse_signature(wrap(signing.encode_signature(SIG))) == SIG


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\n",
        "not base64!",
        base64.b64encode(bytes(63)).decode(),
        base64.b64encode(bytes(65)).decode(),
        base64.b64encode(SIG).decode()[:-1],  # padding cut off
        base64.b64encode(SIG).decode()[:40] + " " + base64.b64encode(SIG).decode()[40:],  # blank inside
        base64.b64encode(SIG).decode() + "A",
        "ü" * 88,
        b"\xff\xfe",
    ],
)
def test_parse_signature_refuses_anything_else(text):
    with pytest.raises(ValueError):
        signing.parse_signature(text)


# --- the release check ---

TAG, ASSET, SHA = "v.0.1.9", "MourseDecoder.exe", "ab" * 32


def release_signature(seed=SEED, tag=TAG, asset=ASSET, sha=SHA):
    return signing.encode_signature(signing.sign(seed, signing.build_manifest(tag, asset, sha)))


@pytest.fixture
def pinned(monkeypatch):
    monkeypatch.setattr(signing, "PINNED_PUBLIC_KEYS", (PUBLIC,))


def reason(*args):
    with pytest.raises(SignatureError) as caught:
        signing.verify_release(*args)
    return caught.value.reason


def test_a_good_release_signature_passes(pinned):
    assert signing.verify_release(TAG, ASSET, SHA, release_signature()) is None
    assert signing.verify_release(TAG, ASSET, SHA.upper(), release_signature().encode()) is None


def test_without_pinned_keys_nothing_passes(monkeypatch):
    monkeypatch.setattr(signing, "PINNED_PUBLIC_KEYS", ())
    assert reason(TAG, ASSET, SHA, release_signature()) == "no_pinned_key"
    assert reason(TAG, ASSET, SHA, "garbage") == "no_pinned_key"  # the missing key is reported first


@pytest.mark.parametrize("text", ["", "garbage", base64.b64encode(bytes(10)).decode()])
def test_a_malformed_signature_file_is_a_format_error(pinned, text):
    assert reason(TAG, ASSET, SHA, text) == "bad_format"


@pytest.mark.parametrize(
    "changed",
    [
        {"tag": "v.0.1.8"},  # an older release signed by the same key: no downgrade
        {"asset": "Other.exe"},
        {"sha": "cd" * 32},
    ],
    ids=["tag", "asset", "hash"],
)
def test_a_signature_for_something_else_is_refused(pinned, changed):
    args = {"tag": TAG, "asset": ASSET, "sha": SHA}
    assert reason(*args.values(), release_signature(**changed)) == "bad_signature"
    assert reason(*{**args, **changed}.values(), release_signature()) == "bad_signature"


def test_a_signature_of_another_key_is_refused(pinned):
    assert reason(TAG, ASSET, SHA, release_signature(seed=signing.generate_seed())) == "bad_signature"


def test_a_tag_with_a_line_break_is_a_format_error(pinned):
    assert reason("v1\n" + ASSET, "x", SHA, release_signature()) == "bad_format"


def test_the_second_pinned_key_may_be_the_one_that_signed(monkeypatch):
    # key rotation: the new key is shipped next to the old one
    other = signing.public_key(signing.generate_seed())
    for keys in ((other, PUBLIC), (PUBLIC, other)):
        monkeypatch.setattr(signing, "PINNED_PUBLIC_KEYS", keys)
        assert signing.verify_release(TAG, ASSET, SHA, release_signature()) is None
    monkeypatch.setattr(signing, "PINNED_PUBLIC_KEYS", (other, signing.public_key(bytes(32))))
    assert reason(TAG, ASSET, SHA, release_signature()) == "bad_signature"


def test_errors_are_update_errors_with_texts_from_the_language_file(monkeypatch):
    monkeypatch.setattr(signing, "PINNED_PUBLIC_KEYS", ())
    with pytest.raises(UpdateError, match="contains no key") as caught:
        signing.verify_release(TAG, ASSET, SHA, "x")
    assert caught.value.open_page
    monkeypatch.setattr(signing, "PINNED_PUBLIC_KEYS", (PUBLIC,))
    for text in ("garbage", release_signature(sha="cd" * 32)):
        with pytest.raises(UpdateError, match="signature of the downloaded update does not match") as caught:
            signing.verify_release(TAG, ASSET, SHA, text)
        assert not caught.value.open_page  # a download page would not help here


def test_the_shipped_keys_are_raw_32_byte_keys():
    assert isinstance(signing.PINNED_PUBLIC_KEYS, tuple)
    assert signing.PINNED_PUBLIC_KEYS  # a release without a pinned key cannot install any update
    assert all(isinstance(k, bytes) and len(k) == 32 for k in signing.PINNED_PUBLIC_KEYS)
    assert all(signing._decode(k) is not None for k in signing.PINNED_PUBLIC_KEYS)
    for key in signing.PINNED_PUBLIC_KEYS:  # verify() refuses small-order keys, so a pinned one must have prime order
        point = signing._decode(key)
        assert not signing._same(point, signing._ZERO)
        assert signing._same(signing._mul(signing._Q, point), signing._ZERO)
