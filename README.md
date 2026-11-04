# weft-mods-index

The curated, signed index of reviewed [Weft](https://github.com/Weft-Rust/Weft) mods. Weft Launcher reads it to offer mods with the **Reviewed** badge.

| File | URL |
|---|---|
| Index | <https://zorro909.github.io/weft-mods-index/v1/index.json> |
| Signature | <https://zorro909.github.io/weft-mods-index/v1/index.json.minisig> |
| Public key | [`keys/index.pub`](keys/index.pub), embedded in the launcher |

Each entry pins one mod release: the sha256 of its `<id>.zip`, the source repo and commit it was built from, the Weft commit and mod ABI it was built against, and who reviewed it. CI builds `index.json` from the files in this repository, signs it with minisign, and publishes it to GitHub Pages.

## Trust model

1. A Weft mod is native code. It runs with the player's full privileges, and no badge changes that.
2. The index proves **where a file came from** and **that it hasn't changed**. It can't prove that a mod is harmless.
3. A reviewer adds each version in a pull request. The PR shows the source commit, so the reviewer can read what they approve.
4. Each entry pins the zip's sha256. The launcher refuses any other bytes, even from the same URL.
5. Downloads may only come from `https://github.com/<source.repo>/releases/download/`.
6. CI re-downloads and re-hashes every pinned asset before it publishes, and again every week.
7. When a release has a build provenance attestation, CI runs `gh attestation verify` and pins the workflow and ref that built it.
8. CI signs the exact bytes of `index.json` with a minisign key. The secret key exists only as a secret of the `index-signing` environment, which only `main` can deploy to.
9. The launcher rejects an index whose signature doesn't verify against its embedded key, and keeps its last verified copy. Taking over a mod repo or a GitHub account can't change the pinned bytes without that key.
10. `revoked` lists sha256 values that must never be installed. The launcher turns those mods off where they are installed.

## Repository layout

```
weft.toml                            Weft releases and the mod ABI of each      -> weft[]
revoked.toml                         revoked builds                             -> revoked[]
mods/<id>/mod.toml                   name, summary, authors, license, homepage, icon
mods/<id>/versions/<version>.toml    one reviewed release                       -> versions[]
keys/index.pub                       the minisign public key
weftindex/                           the tooling (Python 3.11+, standard library only)
tests/                               unit tests
```

A version file looks like this ([example](mods/cogwork/versions/0.2.0.toml)):

```toml
version = "0.2.0"
channel = "release"
tag = "cogwork-v0.2.0"
published = "2026-11-04T02:39:03Z"
deps = [{ id = "base" }]
platforms = ["windows-x64", "linux-x64", "macos-arm64"]

[source]
repo = "Zorro909/weft-mods"
commit = "cceea7805df5902d62017d2008d1bb29ed84e152"

[weft]
tag = "v26.11.1"
commit = "ff9c7128511fc0dce7719b67c9a59274d70bdd5a"
abi = "f470b1cbb1f9ad91"

[asset]
name = "cogwork.zip"
url = "https://github.com/Zorro909/weft-mods/releases/download/cogwork-v0.2.0/cogwork.zip"
size = 2642469
sha256 = "989b21912db1c53d06460cf7bbf9a979ce461c3fe197215124851a427bf7d20c"

[reviewed]
by = "Zorro909"
at = "2026-11-04"
```

- **`version`** is the tag without `<id>-v`. An experimental build's version therefore keeps its `-experimental.<weft7>.<mod7>` suffix, even though its library reports the plain version.
- **`channel`**:
  - `experimental` for an `-experimental.` tag;
  - `prerelease` for a GitHub prerelease or a SemVer prerelease such as `-rc.1`;
  - `release` otherwise.
- **`weft.tag`** is the Weft release tag at `weft.commit`. If that commit isn't a release, it's the `git describe --tags` name, for example `v26.11.1-46-g4563878`.
- **`weft.abi`** is the FNV-1a-64 hash of `crates/abi/src/lib.rs` at `weft.commit`. A mod loads only into a Weft with the same ABI.
- **`[provenance]`** is present only when the asset has a build attestation that verified.

## Index format

The launcher's contract is `docs/mod-manager/INDEX_FORMAT.md` in Weft Launcher. `python3 -m weftindex build` enforces it, and fails on:

- an id that doesn't match `^[a-z0-9_]+$`;
- an asset name other than `<id>.zip`;
- an asset URL other than `https://github.com/<source.repo>/releases/download/<tag>/<id>.zip`;
- a Weft tag that doesn't match `v[0-9A-Za-z.\-]+`;
- an ABI that isn't 16 lowercase hex;
- a sha256 that isn't 64 lowercase hex, here or in `revoked`;
- a duplicate version, tag or sha256;
- a summary that is longer than 140 characters or isn't one line;
- a channel that doesn't match the version;
- an unknown key (a typo is an error, not a silently dropped field).

The output is deterministic:

- mods are sorted by id;
- versions are newest first, by `published`;
- `weft[]` is newest release first;
- the JSON has two-space indentation, UTF-8, and a trailing newline;
- `generated` is the time of the commit that was built.

## Submitting a mod

Requirements:

- The mod is released on GitHub as one `<id>.zip` per release, tagged `<id>-v<version>`.
- The zip holds `<id>.dll`, `lib<id>.so` and/or `lib<id>.dylib`, plus `assets/`.
- The source at the tagged commit is public.
- The release says which Weft commit it was built against. Use one of these:
  - a `<id>.mod.json` asset (see the launcher's index format);
  - the line `Built against Weft <version>, commit [<sha7>](https://github.com/Weft-Rust/Weft/commit/<sha40>)` in the release notes.
- Recommended: an `actions/attest-build-provenance` attestation for `<id>.zip`.

Then:

```sh
python3 -m weftindex add-release <owner>/<repo> <id>-v<version> [more tags...]
python3 -m weftindex build          # validate
python3 -m unittest discover -s tests -t .
git switch -c add-<id>-<version> && git add mods weft.toml && git commit -m "add <id> <version>"
gh pr create
```

`add-release`:

1. Reads the GitHub release.
2. Downloads the zip, hashes it, and requires the hash to equal GitHub's asset `digest`.
3. Resolves the tag to its commit.
4. Finds the Weft commit, from `<id>.mod.json` or the release notes.
5. Hashes Weft's ABI file at that commit.
6. Reads the platforms from the library headers in the zip, and the deps from `<id>.mod.json` or the `declare_mod!` in `src/lib.rs`.
7. If the asset has an attestation, runs `gh attestation verify`.
8. Writes `mods/<id>/versions/<version>.toml`.
9. For a new mod, also writes `mod.toml`, from the lang file (`mod.<id>.name`, `.description`, `.authors`) and `assets/textures/gui/icon.png` at the source commit.

Edit `mod.toml` by hand if the generated summary reads badly. `--update-meta` regenerates it from a newer release. `add-release` needs a GitHub token (`GH_TOKEN`, or a logged-in `gh`); verifying an attestation needs `gh` 2.49 or newer.

## Review

A reviewer approves a PR only after checking these:

- **The source.** Open `https://github.com/<source.repo>/tree/<source.commit>` and read the mod's code, at least what changed since the last reviewed version. Watch for network access, process spawning, file access outside the game's folders, and obfuscated or vendored binaries.
- **The build.**
  - With `[provenance]`: the attestation proves which workflow built the zip, and from which commit. Check that the workflow builds from source.
  - Without it: the reviewer is trusting the release author's build.
- **The metadata.** The name, summary and icon are fine to show to players, and `deps` and `platforms` look right.
- **CI is green.** The `Check` workflow re-downloads and re-verifies every version file the PR touches.

`reviewed.by` is the reviewer's GitHub login. Merging to `main` publishes.

## How the launcher verifies

1. Downloads `index.json` and `index.json.minisig`, and verifies the signature over the exact bytes with the embedded public key. If it fails, it keeps the last verified index.
2. Checks the format rules: id, asset name, asset URL prefix, and ABI format.
3. Offers only versions whose `weft.abi` equals the ABI of the player's Weft version. `weft[]` gives that ABI per release, so the launcher doesn't have to hash it.
4. Downloads `asset.url`, which must be under `https://github.com/<source.repo>/releases/download/`, and installs it only if its sha256 equals `asset.sha256`.
5. Never installs a sha256 listed in `revoked`, and turns installed copies off.

## Revoking a build

Add an entry to `revoked.toml` and merge it:

```toml
[[revoked]]
sha256 = "989b21912db1c53d06460cf7bbf9a979ce461c3fe197215124851a427bf7d20c"
reason = "Shipped a debug build that deletes saves"
at = "2026-11-05"
```

Keep the version file, so the launcher can tell players which mod was revoked. CI stops re-checking revoked assets.

## Rotating the signing key

Only the launcher's embedded key matters. A rotation therefore needs a launcher release, and the old key keeps signing until most players have that release.

1. Make a new key: `minisign -G -p keys/index.pub.new -s index.key`, with a strong password.
2. Ship a launcher release that accepts both the old and the new public key.
3. Replace the environment secrets:

   ```sh
   gh secret set MINISIGN_SECRET_KEY --env index-signing < index.key
   gh secret set MINISIGN_PASSWORD --env index-signing
   ```

4. Move `keys/index.pub.new` to `keys/index.pub`, and merge. The next publish is signed with the new key.
5. In a later launcher release, drop the old key.

If the key leaks:

1. Do steps 1, 3 and 4 at once.
2. Ship a launcher release with only the new key.
3. Until players update, an attacker with the old key can serve a forged index. Pinned sha256 values and the HTTPS download host still limit what they can serve.

## Tooling

```
python3 -m weftindex build [--out dist] [--generated <RFC 3339>]  validate, write dist/v1/index.json
python3 -m weftindex validate                                    validate only
python3 -m weftindex add-release <repo> <tag>...                 write version files
python3 -m weftindex sync-weft                                   add new Weft releases to weft.toml
python3 -m weftindex check [--since <ref>]                       re-download and re-verify (online)
python3 -m unittest discover -s tests -t .                       unit tests
```
