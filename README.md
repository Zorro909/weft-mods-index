# weft-mods-index

The curated, signed index of reviewed [Weft](https://github.com/Weft-Rust/Weft) mods. Weft Launcher reads it to offer mods with the **Reviewed** badge.

| File | URL |
|---|---|
| Index | <https://zorro909.github.io/weft-mods-index/v1/index.json> |
| Signature | <https://zorro909.github.io/weft-mods-index/v1/index.json.minisig> |
| Public key | [`keys/index.pub`](keys/index.pub), embedded in the launcher |

Each entry pins one release of a mod: the sha256 of its zip, the commit it was built from, the Weft commit and mod ABI it was built against, and who reviewed it. When a pull request is merged, CI builds `index.json` from this repository, signs it, and publishes it.

This page is for modders. Reviewers and maintainers: see [MAINTAINING.md](MAINTAINING.md).

## Adding a release

### 1. Make the release

The release must be on GitHub, in a public repository. The [weft-mod-action](https://github.com/Zorro909/weft-mod-action) actions build and release your mods in this form, and can open the pull request of step 2 for you. Without them, a release needs:

- **Tag:** `<id>-v<version>`, where `<id>` matches `[a-z0-9_]+` and `<version>` is SemVer, for example `cogwork-v0.2.0` or `cogwork-v0.3.0-rc.1`.
- **Asset `<id>.zip`:** it holds `<id>.dll`, `lib<id>.so` and/or `lib<id>.dylib` at the top level, and `assets/`.
- **Asset `<id>.mod.json`:** which Weft the zip was built against, its deps, and the mod's name and summary. See [below](#idmodjson).
- **Optional:** an [`actions/attest-build-provenance`](https://github.com/actions/attest-build-provenance) attestation for `<id>.zip`, made by the workflow run that built it. It lets the reviewer trust the build, not just the source.

### 2. Open the pull request

With weft-mod-action, run its [`index-pr` workflow](https://github.com/Zorro909/weft-mod-action#proposing-to-the-index) from your repository's Actions tab instead: it does all of this step for you.

By hand, you need Python 3.11 or newer and the [GitHub CLI](https://cli.github.com/) (`gh`), logged in with `gh auth login`. If your release has an attestation, you need `gh` 2.49 or newer.

```sh
gh repo fork Zorro909/weft-mods-index --clone && cd weft-mods-index    # the first time
git fetch upstream && git switch -c add-cogwork-0.2.0 upstream/main     # every time

python3 -m weftindex add-release <owner>/<repo> <id>-v<version>    # several tags at once are fine
```

Run it from the root of your clone. Branching from `upstream/main` starts from what the index has now, not from your fork's old copy. A clone made some other way may lack the `upstream` remote: `git remote add upstream https://github.com/Zorro909/weft-mods-index.git`.

`add-release` reads the release, downloads and hashes the zip, and checks everything it finds against everything else. Then it writes:

- `mods/<id>/versions/<version>.toml`, one per tag;
- `mods/<id>/mod.toml`, the name, summary, authors and icon, for a mod that is new to the index. Pass `--update-meta` to rewrite it from a newer release;
- `weft.toml`, if your mod was built against a Weft release the index doesn't list yet.

It writes these only if every tag succeeds and the repository validates with them. At the end it prints the commands that commit, push and open the pull request, with a title and a description (`dist/pr.md`) that link your releases and the source changes for the reviewer:

```sh
git add mods weft.toml && git commit -m 'Add cogwork 0.2.0'
git push -u origin HEAD
gh pr create --title 'Add cogwork 0.2.0' --body-file dist/pr.md
```

Don't edit the version files. You may reword `mod.toml`'s `summary` (one line, 140 characters at most) if the generated one reads badly. `[reviewed]` holds your login for now; the reviewer replaces it with theirs when they approve.

### 3. Wait for Check and a review

The `Check` workflow runs on the PR. It validates every file, and re-downloads and re-verifies each version file the PR adds. A reviewer reads the mod's source at the pinned commit (see [MAINTAINING.md](MAINTAINING.md#review)), and merging publishes the index.

### If something fails

Each error names the file and the field. The usual causes:

| Error | Fix |
|---|---|
| `release <tag> has no <id>.mod.json` | Upload one to the release (assets can be added to a published release). Then run `add-release` again. |
| `downloaded sha256 …, GitHub's digest is …` or `… is pinned as …, but the release now gives …` | The asset or tag changed after the version file was written. Re-upload the original, or make a new release and add that instead. |
| `<id>.mod.json: <field> is …, not …` | `<id>.mod.json` disagrees with what the tool found in the release, in the field it names. Regenerate it in the release workflow. |
| `weft.tag '…' doesn't name Weft …` | `weft.tag` must be the release tag at `weft.commit`, or its `git describe --tags` name, such as `v26.11.1-46-g4563878`. |
| `verifying an attestation needs gh 2.49 or newer` | Update `gh`. |
| `mods/<id>/versions/<version>.toml exists` | That version is already in the index. `--force` overwrites it, but only use it when the release itself was fixed. |
| `HTTP 401` / `HTTP 403` | Log in with `gh auth login`, or set `GH_TOKEN`. |

## `<id>.mod.json`

An asset that describes the release, in the format of `docs/mod-manager/INDEX_FORMAT.md` in Weft Launcher. `add-release` takes the Weft commit and tag, the deps and the mod's metadata from it. It refuses the release if any other field disagrees with what it finds (`published` aside), or if `weft.tag` doesn't name `weft.commit`:

```json
{
  "schema": 1,
  "id": "cogwork",
  "name": "Cogwork",
  "summary": "Gears, item tubes and engines.",
  "authors": ["Zorro909"],
  "license": "MIT",
  "homepage": "https://github.com/Zorro909/weft-mods/tree/main/cogwork",
  "icon": "https://raw.githubusercontent.com/Zorro909/weft-mods/<commit>/cogwork/assets/textures/gui/icon.png",
  "version": "0.2.0",
  "channel": "release",
  "tag": "cogwork-v0.2.0",
  "source": { "repo": "Zorro909/weft-mods", "commit": "<the tag's commit, 40 hex>" },
  "weft": { "tag": "v26.11.1", "commit": "<40 hex>", "abi": "<FNV-1a-64 of crates/abi/src/lib.rs, 16 hex>" },
  "deps": [{ "id": "base" }],
  "platforms": ["windows-x64", "linux-x64", "macos-arm64"],
  "asset": { "name": "cogwork.zip", "url": "https://github.com/…/cogwork.zip", "size": 2642469, "sha256": "<64 hex>" }
}
```

`license`, `homepage` and `icon` may be `null`. `channel` is `experimental` for an `-experimental.` version; `prerelease` for any other SemVer prerelease, such as `-rc.1`; and `release` otherwise. It depends on the version alone, not on GitHub's prerelease flag. weft-mod-action's [`release`](https://github.com/Zorro909/weft-mod-action#releasing) action writes one.

## What a version file says

For example:

```toml
version = "0.2.0"                       # the tag without "<id>-v"
channel = "release"                     # release, prerelease or experimental (see above)
tag = "cogwork-v0.2.0"
published = "2026-11-04T02:39:03Z"      # when GitHub published the release
deps = [{ id = "base" }]
platforms = ["windows-x64", "linux-x64", "macos-arm64"]   # from the library headers in the zip

[source]
repo = "Zorro909/weft-mods"
commit = "cceea7805df5902d62017d2008d1bb29ed84e152"       # the tag's commit

[weft]
tag = "v26.11.1"                        # or `git describe --tags`, e.g. v26.11.1-46-g4563878
commit = "ff9c7128511fc0dce7719b67c9a59274d70bdd5a"
abi = "f470b1cbb1f9ad91"                # FNV-1a-64 of crates/abi/src/lib.rs at weft.commit

[asset]
name = "cogwork.zip"
url = "https://github.com/Zorro909/weft-mods/releases/download/cogwork-v0.2.0/cogwork.zip"
size = 2642469
sha256 = "989b21912db1c53d06460cf7bbf9a979ce461c3fe197215124851a427bf7d20c"

# [provenance] is here only when the zip has an attestation that verified.

[reviewed]
by = "Zorro909"
at = "2026-11-04"
```

A mod loads only into a Weft whose ABI is the same as `weft.abi`, so the launcher offers each version only to players on a matching Weft.

## Trust model

1. A Weft mod is native code. It runs with the player's full privileges, and no badge changes that.
2. The index proves **where a file came from** and **that it hasn't changed**. It can't prove that a mod is harmless; that is what the human review is for.
3. Each entry pins the zip's sha256. The launcher refuses any other bytes, even from the same URL, and downloads only from `https://github.com/<source.repo>/releases/download/`.
4. CI re-downloads and re-verifies every pinned release before every publish, and at least weekly.
5. With an attestation, CI also verifies which workflow built the zip, and that it built the tagged commit or its parent.
6. CI signs the exact bytes of `index.json` with a minisign key that only the signing job of `main` can use, and that job runs no code that parses release assets. The launcher rejects an index whose signature doesn't verify, so taking over a mod repository or a GitHub account can't change what is pinned.
7. A revoked build is never installed, and the launcher turns it off where it is installed.

## Repository layout

```
mods/<id>/mod.toml                   name, summary, authors, license, homepage, icon (no mods/ yet: the index is empty)
mods/<id>/versions/<version>.toml    one reviewed release
weft.toml                            Weft releases and the mod ABI of each
revoked.toml                         revoked builds
keys/index.pub                       the minisign public key
weftindex/                           the tooling (Python 3.11+, standard library only)
tests/                               unit tests: python3 -m unittest discover -s tests -t .
```

`python3 -m weftindex --help` lists the commands.
