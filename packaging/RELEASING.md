# Releasing MourseDecoder

Updates are checked in the app against an ed25519 public key that is compiled into it
(`PINNED_PUBLIC_KEYS` in `python/mourse_decoder/signing.py`). The private key is only on the
maintainer's machine. It is never in GitHub, in CI or in a secret. The CI builds the exe and the
MSI installer and creates a draft release; the maintainer signs both offline and publishes.

All commands below are for PowerShell 5.1, run from the repository root, one per line.
`python` is the project's own interpreter (for example `.venv\Scripts\python.exe`).

## a) One-time setup

1. Create the key outside the repository. The command refuses an existing file and any path inside the repository.

        .venv\Scripts\python.exe packaging\sign_release.py keygen --out D:\keys\mourse-update.key

   It prints only the public key (64 hex characters). The seed stays in the file.
2. Back up the key file offline (for example an encrypted USB stick kept elsewhere). Without it no
   further release can be signed for the existing installs.
3. Paste the public key as 32 raw bytes into `PINNED_PUBLIC_KEYS` in `python/mourse_decoder/signing.py`,
   for example `bytes.fromhex("<printed hex>")`. Check it later with:

        .venv\Scripts\python.exe packaging\sign_release.py pubkey --key D:\keys\mourse-update.key

4. Release a version that contains the key (procedure b). Until the key is pinned, the release workflow
   fails on its gate step ("no pinned update key"), and installed apps accept no update.

What the key file protection does: the file is created with mode 0600 on POSIX. On Windows the script
creates the empty file, runs `%SystemRoot%\System32\icacls.exe <file> /reset`, then
`icacls.exe <file> /inheritance:r /grant:r *<your SID>:F` (the SID comes from
`%SystemRoot%\System32\whoami.exe /user`), and only then writes the seed. `/reset` removes the explicit
entries (SYSTEM, Administrators, OWNER RIGHTS) that a new file gets from its creator when the folder has no
inheritable entries; it has to be a separate call. Inherited permissions are removed too, so other accounts
lose access, and the seed is never in a file with inherited permissions. The script then lists the file's
entries with `icacls.exe <file>` and warns if there is more than one. This is best effort: administrators,
backup tools, malware running as you, and a copy to another disk are not covered. The key is not encrypted.
If an `icacls` call fails or other entries remain, the script prints a warning on stderr, still writes the
key, and you have to fix the permissions by hand.

## b) Every release

1. Bump the version in `Cargo.toml`, `pyproject.toml` and `python/mourse_decoder/__init__.py`, merge to `main`.
2. Dry run first: run the workflow from the release branch and wait until it is green. It runs the tests, builds
   and validates the MSI and runs the install, upgrade and uninstall smoke test. The check of the tag against the
   version files, the check for a pinned key and the publish step run only for a `v*` tag, so nothing is released:

        git push origin release-v.0.1.10
        gh workflow run release.yml --ref release-v.0.1.10
        gh run watch

   Then push the tag (the form is `v.0.1.10`; the release workflow compares it with the three version files):

        git tag v.0.1.10
        git push origin v.0.1.10

   The workflow tests, builds `MourseDecoder.exe` and `MourseDecoder.msi`, runs an install, upgrade and
   uninstall of the MSI, and creates a DRAFT release with both files and their `.sha256` files. Users do not
   see a draft.
3. Download the exe and the MSI from the draft into an empty folder:

        gh release download v.0.1.10 --pattern MourseDecoder.exe --pattern MourseDecoder.exe.sha256 --pattern MourseDecoder.msi --pattern MourseDecoder.msi.sha256 --dir C:\release-v.0.1.10

4. Compare the hashes with the published `.sha256` files. Each contains `<hash>  <file name>`; the hash part
   (the first 64 characters) must be identical to the output of the matching command:

        (Get-FileHash C:\release-v.0.1.10\MourseDecoder.exe -Algorithm SHA256).Hash.ToLower()
        Get-Content C:\release-v.0.1.10\MourseDecoder.exe.sha256
        (Get-FileHash C:\release-v.0.1.10\MourseDecoder.msi -Algorithm SHA256).Hash.ToLower()
        Get-Content C:\release-v.0.1.10\MourseDecoder.msi.sha256

   If you do not trust the draft, also compare against the `MourseDecoder-windows` workflow artifact, or
   rebuild the exe and the MSI yourself. A matching `.sha256` is not proof, because both come from the same GitHub
   release. Signing the file you downloaded states "I accept this exact file".
5. Put the working tree on the tag. `sign` and `verify` read `PINNED_PUBLIC_KEYS` from the working tree, so
   it must be the tree that was tagged, not a newer branch state:

        git fetch --tags
        git switch --detach v.0.1.10

   To see the key that the tag contains, independent of the working tree:

        git show v.0.1.10:python/mourse_decoder/signing.py

   The script helps with this: `sign` and `verify` run `git rev-parse` and fail if the local tag does not
   exist exactly as typed, and they print a warning if `HEAD` is not the tag's commit. `--no-git-check`
   skips this (use it if git is not installed). The warning does not catch uncommitted changes to
   `signing.py` or to any other file. Run `git status` before you sign: it must show a clean tree
   ("nothing to commit, working tree clean"). It does not list files that `.gitignore` hides, and the
   script could import one of them; `git status --ignored` lists them too, and a fresh clone has none.
   Read the whole diff since the previous release, not only `PINNED_PUBLIC_KEYS`, because the signing
   script imports the repository's own Python package while it holds the key (see e):

        git diff <previous tag> v.0.1.10

   Look hardest at `python/`, `packaging/` and `.github/`. Sign only if you accept every changed line.
   The spelling matters: the app signs over the exact `tag_name` of the GitHub release, and `v.0.1.10` and
   `v0.1.10` are different strings. Use the tag as pushed in step 2.
6. Sign both files. Each command writes `<asset name>.sig` next to the file and verifies it straight away.
   The asset name follows the suffix of `--exe`: `.exe` is signed as `MourseDecoder.exe`, `.msi` as
   `MourseDecoder.msi`. `--asset-name` may repeat it, any other value is refused (exit code 2), and the output
   prints the asset name. The asset name is part of what is signed, so a signature for one
   file is not valid for the other:

        .venv\Scripts\python.exe -I packaging\sign_release.py sign --tag v.0.1.10 --exe C:\release-v.0.1.10\MourseDecoder.exe --key D:\keys\mourse-update.key

        .venv\Scripts\python.exe -I packaging\sign_release.py sign --tag v.0.1.10 --asset-name MourseDecoder.msi --exe C:\release-v.0.1.10\MourseDecoder.msi --key D:\keys\mourse-update.key

   `-I` makes Python ignore `PYTHON*` environment variables and the user site directory, and keeps the
   current directory and the script folder off `sys.path`, so a planted module or variable cannot change what
   runs. It works with this script, which adds `python/` of the checkout to the path itself (run locally with
   `--help`, `keygen`, `pubkey` and `sign` on a throw-away key). It does not protect against malicious code in
   the tagged tree: that is the code the script imports on purpose. Only the diff review and the clean
   checkout from step 5 limit that, see e).

   `sign` prints whether the key is pinned. If the public key of the key file is not in
   `PINNED_PUBLIC_KEYS`, it stops with an error and writes nothing, because installed apps would reject the
   signature. `--allow-unpinned` overrides that; use it only for key rotation preparation (see c) and tests.
7. Check both signatures against the key pinned in the tagged tree:

        .venv\Scripts\python.exe -I packaging\sign_release.py verify --tag v.0.1.10 --exe C:\release-v.0.1.10\MourseDecoder.exe --sig C:\release-v.0.1.10\MourseDecoder.exe.sig

        .venv\Scripts\python.exe -I packaging\sign_release.py verify --tag v.0.1.10 --asset-name MourseDecoder.msi --exe C:\release-v.0.1.10\MourseDecoder.msi --sig C:\release-v.0.1.10\MourseDecoder.msi.sig

8. Upload the signatures and publish, then return to your branch:

        gh release upload v.0.1.10 C:\release-v.0.1.10\MourseDecoder.exe.sig C:\release-v.0.1.10\MourseDecoder.msi.sig

        gh release edit v.0.1.10 --draft=false

        git switch -

Upload `MourseDecoder.exe.sig` and `MourseDecoder.msi.sig` before you publish. Installed apps that update
from the exe (0.1.9 and every exe-only install) need `MourseDecoder.exe`, its `.sha256` and its `.sig`; they
stay on the exe asset, which is why it is still published next to the MSI. A release that is published
without the signature an app needs is refused by that app ("has no signature"), and since the app no longer
offers a download page for such a release, users get neither an update nor a link to the release from the app
until the `.sig` is uploaded.

Per the maintainer, tags are protected by a ruleset and cannot be moved or deleted (not checked here). A
bad build or a failed workflow needs a new version number and a new tag. A wrong or missing signature does
not necessarily need a new version: the files are unchanged, so the `.sig` asset can likely be replaced on the
published release with `gh release upload <tag> <file> --clobber`. This is NOT verified on GitHub. The tag
itself still cannot be moved or deleted.

## c) Key rotation

1. Create the new key as in a), steps 1 and 2.
2. Release an app version whose `PINNED_PUBLIC_KEYS` contains the old and the new public key. Sign that
   release (the exe and the MSI) with the OLD key, because the installed apps only know the old one.
3. Sign later releases with the new key. Once you consider all installs updated to the version from step 2,
   release a version that contains only the new key.

Installs that skip the version from step 2 only know the old key and cannot update to a release signed
with the new key; those users must install manually.

## d) Key loss or compromise

Installed apps trust only the keys compiled into them. They cannot be told to trust a new key remotely,
and that is the point of the pinning.

- Key lost: no release can be signed for existing installs any more. Create a new key, release an app with
  the new key pinned, and tell users to download and install that version by hand.
- Key stolen: whoever holds it can sign an exe that existing installs accept, if they can also get it
  served to the user (a GitHub release, or a network attacker). Existing installs cannot be moved to a new
  key by an update signed with the stolen one in a way you can trust, so create a new key, release an app
  with only the new key pinned, and tell users publicly to reinstall by hand.

The first download of the exe (from the release page or anywhere else) is not protected by the app. The
signature is only checked by an app that is already installed.

## e) What the signature covers and what it does not

Covered: the exe or MSI that an installed app installs is exactly the file you signed, with the key built
into that app. A swapped release asset, an edited release or a changed download link is refused, because the
signature covers the tag, the file name and the SHA-256 of the file the app has downloaded.

Not covered:

- The build. The CI workflow builds the exe, and you sign what the CI produced. A compromised CI, runner,
  build action or build dependency would produce an exe that you then sign. `requirements-build.txt` has no
  hashes, so a changed package on PyPI is not detected. Building the exe on your own machine and signing that
  file would close this; it is a possible later change, not done now. The hash comparison in step 4 only shows
  that the file you downloaded is the one in the draft, not that the build was clean.
- A rollback. The app installs a release only if its tag is newer than the installed version and the
  signature covers that tag. If a newer release exists, someone who can serve an older release that is still
  validly signed, and newer than the installed version, can get that older build installed. The tag in the
  signature only stops a signature from being moved to another tag.
- What is inside the MSI. The MSI hash covers the exe packaged in it, but the MSI is built by the same CI
  run from the same build (`packaging/build_msi.py` with WiX 5.0.2, installed from NuGet in the workflow), so
  everything said above about the CI build applies to it. The install, upgrade and uninstall check in the
  workflow runs against the MSI that is released, but it only looks at files, registry entries and the
  Start menu shortcut, not at whether the exe starts.
- Authenticode. Neither the exe nor the MSI is code-signed, so Windows SmartScreen can warn when a user
  downloads and runs either of them. The signature described here is only checked by an installed app.
- The first installation. A user who downloads a manipulated exe or MSI by hand is not warned.
- The source code on GitHub and the maintainer's machine. Someone who can change the pinned key in the source
  (a compromised repository) before you release gets their key into the build.
- The code that runs while you sign. `sign_release.py` imports the repository's own Python package
  (`mourse_decoder`) while it holds the key, so malicious code in any module of the tagged tree could read the
  key. `keygen` and `pubkey` do the same. This is why step 5 asks for a review of the whole diff since the
  last release and a clean checkout of the tag, and not only a look at `PINNED_PUBLIC_KEYS`.
- A compromised signing machine or a stolen key file (see d).
- Signing an exe you did not check. `sign` signs what you give it.
- Anything that is not `MourseDecoder.exe` or `MourseDecoder.msi`. The `.sha256` files are unsigned and are
  only a transfer check.

## Not verified

The procedure was written without running a real release. The `gh` commands (`download`, `upload`,
`upload --clobber`, `edit` on a draft) and the draft workflow run were not exercised, and the
`PINNED_PUBLIC_KEYS` gate step was only run locally. `keygen`, `sign` and `verify` and the `icacls` call
were run locally on a throw-away key (the ACL then listed only the current user). The git tag check is
covered by tests on temporary git repositories, not by a run against a real release tag. The MSI was built
with WiX 5.0.2 and installed, upgraded and uninstalled once on a development machine with a placeholder
exe, using the same PowerShell checks as the workflow; the workflow steps themselves (setup-dotnet, the
`wix` install, the artifact upload) and the signing of an MSI from a real draft were not run.
