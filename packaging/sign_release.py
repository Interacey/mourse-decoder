"""Offline signing of a release, run by the maintainer on their own machine (never in CI).

    python packaging/sign_release.py keygen --out C:\\keys\\mourse-update.key
    python packaging/sign_release.py pubkey --key C:\\keys\\mourse-update.key
    python packaging/sign_release.py sign --tag v.0.1.10 --exe MourseDecoder.exe --key C:\\keys\\mourse-update.key
    python packaging/sign_release.py verify --tag v.0.1.10 --exe MourseDecoder.exe --sig MourseDecoder.exe.sig

The signature covers the manifest from ``signing.build_manifest`` (tag, the asset name, SHA-256 of the file). The
asset name follows the suffix of ``--exe``: .exe is signed as MourseDecoder.exe, .msi as MourseDecoder.msi, whatever
the local file is called. ``--asset-name`` may repeat that name; a different one is refused (exit code 2), so
signing the msi can never write the signature of the exe.
See packaging/RELEASING.md for the full procedure.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

from mourse_decoder import signing  # noqa: E402

ASSET_NAMES = ("MourseDecoder.exe", "MourseDecoder.msi")
ASSET_OF_SUFFIX = {".exe": ASSET_NAMES[0], ".msi": ASSET_NAMES[1]}
# release.yml strips `^v\.?` and compares the rest with the version files
TAG_RE = re.compile(r"v\.?\d+\.\d+\.\d+")


class UserError(Exception):
    """A problem the user can fix; printed without a traceback."""


class UsageError(UserError):
    """The options contradict each other; like a bad command line, this ends with exit code 2."""


def asset_name_for(path: Path, asset_name: str | None) -> str:
    """The release asset name of ``path``: given by its suffix, and ``asset_name`` may only repeat it."""
    derived = ASSET_OF_SUFFIX.get(path.suffix.lower())
    if asset_name is None:
        if derived is None:
            raise UsageError(f"Cannot tell the asset name from {path.name!r}: it does not end in .exe or .msi")
        return derived
    if asset_name != derived:
        raise UsageError(
            f"--asset-name {asset_name} does not match {path.name!r}: a file ending in {path.suffix or 'nothing'} "
            f"is signed as {derived or 'no known asset'}"
        )
    return asset_name


def sig_name(asset_name: str) -> str:
    return asset_name + ".sig"


def check_tag(tag: str) -> str:
    if not TAG_RE.fullmatch(tag):
        raise UserError(f"Bad tag {tag!r}: expected the form v.0.1.9")
    return tag


def sha256_hex(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError as error:
        raise UserError(f"Cannot read {path}: {error.strerror or error}") from error
    return digest.hexdigest()


def read_seed(path: Path) -> bytes:
    try:
        text = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as error:
        raise UserError(f"Cannot read key file {path}: {error}") from error
    if not re.fullmatch(r"[0-9a-fA-F]{64}", text):
        raise UserError(f"{path} is not a key file (expected 64 hex characters)")
    return bytes.fromhex(text)


def _system32(name: str) -> str:
    """A Windows tool by full path, so a changed PATH can't replace it."""
    root = os.environ.get("SystemRoot")
    if root:
        path = Path(root) / "System32" / name
        if path.is_file():
            return str(path)
    return name


def _current_user_sid() -> str:
    out = subprocess.run(
        [_system32("whoami.exe"), "/user", "/fo", "csv", "/nh"], check=True, capture_output=True, text=True
    ).stdout
    sid = next(csv.reader([out.strip()]))[1]
    if not re.fullmatch(r"S-1-[0-9-]+", sid):
        raise ValueError(f"unexpected SID {sid!r}")
    return sid


def _warn_unrestricted(path: Path, reason: str = "") -> None:
    print(f"Warning: could not restrict access to {path}{reason}; set the permissions by hand.", file=sys.stderr)


def restrict_to_owner(path: Path) -> None:
    """Best effort: POSIX mode 0600 is set at creation; on Windows the ACL is reduced to the current user.

    `/reset` runs as its own call first: it drops the explicit entries (SYSTEM, Administrators, OWNER RIGHTS) that
    a new file gets from the creator's default DACL when the folder has no inheritable entries. `/inheritance:r`
    removes only inherited entries and `/grant:r` replaces only the same SID's entry, so without it they stay.
    The result is checked: any other number of entries than one is reported.
    """
    if os.name != "nt":
        return
    icacls = _system32("icacls.exe")
    try:
        sid = _current_user_sid()
    except (OSError, ValueError, IndexError, StopIteration, subprocess.CalledProcessError):
        _warn_unrestricted(path)
        return
    try:
        subprocess.run([icacls, str(path), "/reset"], check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        _warn_unrestricted(path, " (icacls /reset failed)")
    try:
        subprocess.run([icacls, str(path), "/inheritance:r", "/grant:r", f"*{sid}:F"], check=True, capture_output=True)
        shown = subprocess.run(
            [icacls, str(path)], check=True, capture_output=True, text=True, errors="replace"
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        _warn_unrestricted(path)
        return
    entries = shown.count(":(")  # one "principal:(rights)" per entry, in every Windows language
    if entries != 1:
        _warn_unrestricted(path, f" ({entries} access entries remain, expected 1)")


def git_tag_commit(tag: str) -> str:
    """The commit the local git tag points to. UserError if git is missing or the tag does not exist."""
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}"],
            capture_output=True, text=True,
        )
    except OSError as error:
        raise UserError(f"Cannot run git ({error.strerror or error}); fix that or pass --no-git-check") from error
    if result.returncode == 1:
        raise UserError(
            f"No local git tag {tag!r}. The client signs over GitHub's exact tag_name, so the spelling must match "
            "the pushed tag (git fetch --tags first); or pass --no-git-check"
        )
    if result.returncode != 0:
        raise UserError(f"git failed: {result.stderr.strip()}; fix that or pass --no-git-check")
    return result.stdout.strip()


def check_git_tag(tag: str, warn_head: bool) -> None:
    """The tag must exist exactly as typed; optionally warn if the working tree is not at that commit."""
    commit = git_tag_commit(tag)
    if not warn_head:
        return
    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    if head != commit:
        print(
            f"Warning: HEAD ({head[:12]}) is not the commit of {tag} ({commit[:12]}). PINNED_PUBLIC_KEYS is read "
            f"from the working tree; run `git switch --detach {tag}` first.",
            file=sys.stderr,
        )


def cmd_keygen(args: argparse.Namespace) -> int:
    out = Path(args.out).resolve()
    if out == ROOT or ROOT in out.parents:
        raise UserError(f"Refusing to write the key inside the repository ({ROOT}); use a path outside it")
    seed = signing.generate_seed()
    try:
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise UserError(f"{out} already exists; refusing to overwrite a key") from None
    except OSError as error:
        raise UserError(f"Cannot create {out}: {error.strerror or error}") from error
    # the file is still empty here: restrict it before the seed is written
    restrict_to_owner(out)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(seed.hex() + "\n")
    except OSError as error:
        out.unlink(missing_ok=True)
        raise UserError(f"Cannot write {out}: {error.strerror or error}") from error
    print(signing.public_key(seed).hex())
    return 0


def cmd_pubkey(args: argparse.Namespace) -> int:
    print(signing.public_key(read_seed(Path(args.key))).hex())
    return 0


def cmd_sign(args: argparse.Namespace) -> int:
    tag = check_tag(args.tag)
    if not args.no_git_check:
        check_git_tag(tag, warn_head=True)
    exe = Path(args.exe)
    asset_name = asset_name_for(exe, args.asset_name)
    seed = read_seed(Path(args.key))
    pubkey = signing.public_key(seed)
    pinned = pubkey in signing.PINNED_PUBLIC_KEYS
    if not pinned and not args.allow_unpinned:
        raise UserError(
            f"The public key {pubkey.hex()} of this key file is not in PINNED_PUBLIC_KEYS; installed apps would "
            "reject the signature. Use the right key file, or pass --allow-unpinned (key rotation preparation, tests)"
        )
    digest = sha256_hex(exe)
    manifest = signing.build_manifest(tag, asset_name, digest)
    signature = signing.sign(seed, manifest)
    out = Path(args.out) if args.out else exe.resolve().parent / sig_name(asset_name)
    try:
        out.write_text(signing.encode_signature(signature), encoding="ascii", newline="\n")
        written = signing.parse_signature(out.read_text(encoding="ascii"))
    except (OSError, ValueError) as error:
        raise UserError(f"Cannot write or re-read {out}: {error}") from error
    if not signing.verify(pubkey, written, manifest):
        out.unlink(missing_ok=True)
        raise UserError("The written signature does not verify; it was deleted")
    print(f"tag:        {tag}")
    print(f"asset:      {asset_name}")
    print(f"sha256:     {digest}")
    print(f"public key: {pubkey.hex()} ({'pinned' if pinned else 'NOT pinned'})")
    print(f"signature:  {out} (verified)")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    tag = check_tag(args.tag)
    asset_name = asset_name_for(Path(args.exe), args.asset_name)
    if not args.no_git_check:
        check_git_tag(tag, warn_head=not args.pubkey)
    if args.pubkey:
        try:
            keys = (bytes.fromhex(args.pubkey),)
        except ValueError:
            raise UserError("--pubkey is not valid hex") from None
        if len(keys[0]) != 32:
            raise UserError("--pubkey must be 32 bytes (64 hex characters)")
    else:
        keys = tuple(signing.PINNED_PUBLIC_KEYS)
        if not keys:
            raise UserError("No key is pinned in PINNED_PUBLIC_KEYS; pass --pubkey <hex>")
    digest = sha256_hex(Path(args.exe))
    try:
        signature = signing.parse_signature(Path(args.sig).read_text(encoding="ascii"))
    except (OSError, ValueError) as error:
        raise UserError(f"Cannot read signature {args.sig}: {error}") from error
    manifest = signing.build_manifest(tag, asset_name, digest)
    if any(signing.verify(key, signature, manifest) for key in keys):
        print(f"OK: signature is valid for {tag}, {asset_name}, sha256 {digest}")
        return 0
    print(f"FAILED: signature does not match {tag} with sha256 {digest} for the given key(s)", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sign and verify MourseDecoder releases (offline).")
    sub = parser.add_subparsers(dest="command", required=True)

    keygen = sub.add_parser("keygen", help="create a new signing key and print its public key")
    keygen.add_argument("--out", required=True, help="new key file, outside the repository")
    keygen.set_defaults(func=cmd_keygen)

    pubkey = sub.add_parser("pubkey", help="print the public key of a key file")
    pubkey.add_argument("--key", required=True)
    pubkey.set_defaults(func=cmd_pubkey)

    sign = sub.add_parser("sign", help="write the .sig file for a downloaded release asset")
    sign.add_argument("--tag", required=True, help="release tag, e.g. v.0.1.9")
    sign.add_argument("--exe", required=True, help="the downloaded exe or msi")
    sign.add_argument("--asset-name", choices=ASSET_NAMES, help="must match the suffix of --exe (default)")
    sign.add_argument("--key", required=True)
    sign.add_argument("--out", help="signature file (default: <asset name>.sig next to the file)")
    sign.add_argument("--allow-unpinned", action="store_true", help="sign even if the key is not in PINNED_PUBLIC_KEYS")
    sign.add_argument("--no-git-check", action="store_true", help="do not check that the git tag exists locally")
    sign.set_defaults(func=cmd_sign)

    verify = sub.add_parser("verify", help="check a signature against the pinned key(s)")
    verify.add_argument("--tag", required=True)
    verify.add_argument("--exe", required=True, help="the downloaded exe or msi")
    verify.add_argument("--asset-name", choices=ASSET_NAMES, help="must match the suffix of --exe (default)")
    verify.add_argument("--sig", required=True)
    verify.add_argument("--pubkey", help="hex public key instead of PINNED_PUBLIC_KEYS")
    verify.add_argument("--no-git-check", action="store_true", help="do not check that the git tag exists locally")
    verify.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except UsageError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    except (UserError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
