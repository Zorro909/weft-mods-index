# weft-mods-index

The curated index of reviewed [Weft](https://github.com/Weft-Rust/Weft) mods. Weft Launcher reads it to offer mods with the **Reviewed** badge:

**<https://weft-rust.github.io/weft-mods-index/v2/index.json>**

Each entry pins one release of a mod by the sha256 of its zip. A pull request adds the entry and a reviewer reads the mod's source before merging. On merge, CI downloads every release, verifies its build provenance attestation, reads its `weft-mod.toml`, and publishes `index.json`. Nobody writes `index.json` by hand.

This page is for modders. Reviewers and maintainers: see [MAINTAINING.md](MAINTAINING.md).

## Adding a release

### 1. Make the release

The [weft-mod-action](https://github.com/Zorro909/weft-mod-action) actions build and release your mods in the right form. A release must be on GitHub, in a public repository, and have:

- **Tag:** `<id>-v<version>`, where `<id>` matches `[a-z0-9_]{1,64}` and `<version>` is SemVer, for example `cogwork-v0.2.0` or `cogwork-v0.3.0-rc.1`.
- **Asset `<id>.zip`.** It holds:
  - `weft-mod.toml` (see below);
  - `<id>.dll`, `lib<id>.so` and/or `lib<id>.dylib` at the top level;
  - `assets/...`, and anything else the mod needs. CI only refuses paths that could land outside the mod's folder (`..`, absolute paths, backslashes).
- **Asset `<id>.mod.json`:** `{"attestation": <bundle>}`. The bundle is the Sigstore bundle that [`actions/attest-build-provenance`](https://github.com/actions/attest-build-provenance) writes for `<id>.zip`, in a workflow of the same repository.

`weft-mod.toml`:

```toml
id = "cogwork"
name = "Cogwork"
version = "0.2.0"          # the tag's version
abi = "f470b1cbb1f9ad91"   # FNV-1a-64 of Weft's crates/abi/src/lib.rs that the mod was built against
deps = ["base"]            # mod ids; each must be "base" or a mod in the index
```

### 2. Open the pull request

With weft-mod-action, run its [`index-pr` workflow](https://github.com/Zorro909/weft-mod-action#proposing-to-the-index). It does this step for you.

By hand, you need Python 3.11 or newer and [`gh`](https://cli.github.com/) 2.49 or newer, logged in:

```sh
gh repo fork Weft-Rust/weft-mods-index --clone && cd weft-mods-index   # the first time
git fetch upstream && git switch -c add-cogwork-0.2.0 upstream/main    # every time

python3 -m weftindex add-release <owner>/<repo> <id>-v<version> [more tags] [--summary "..."]
```

`add-release` downloads each release, verifies it as CI will, and writes its version file. Then it prints the commands that commit, push and open the PR. Its description, in `dist/pr.md`, links the source of each build for the reviewer. A mod that is new to the index also needs `--summary`.

### 3. Wait for Check and a review

The `Check` workflow verifies every release on the PR. A reviewer reads your mod's source at the commit each build was made from. Merging publishes the index.

## The files

```
mods/<id>/mod.toml                   the mod's summary, and optionally a name
mods/<id>/versions/<version>.toml    one release: repo, tag, sha256
weft.toml                            Weft releases and the mod ABI of each
revoked.toml                         revoked builds
weftindex/                           the tooling (Python 3.11+, standard library only)
tests/                               python3 -m unittest discover -s tests -t .
```

`mods/<id>/mod.toml`:

```toml
summary = "Gears, item tubes and engines."   # one line, at most 140 characters
# name = "Cogwork"                           # optional; default: the newest build's weft-mod.toml
```

`mods/<id>/versions/<version>.toml`:

```toml
repo = "Zorro909/weft-mods"
tag = "cogwork-v0.2.0"
sha256 = "989b21912db1c53d06460cf7bbf9a979ce461c3fe197215124851a427bf7d20c"   # of <id>.zip
```

All versions of a mod come from one repo. CI copies `abi` and `deps` from the zip's `weft-mod.toml` into the index. It refuses a build if:

- the zip's sha256 isn't the one pinned;
- the attestation doesn't verify;
- `weft-mod.toml` disagrees with the file's mod id or version;
- a dep isn't `base` or a mod in the index.

## `index.json`

```json
{
  "schema": 2,
  "weft": { "v26.11.2": "d4cb65bd4fa1ac78", "v26.11.1": "f470b1cbb1f9ad91" },
  "mods": [
    { "id": "cogwork", "name": "Cogwork", "summary": "Gears, item tubes and engines.",
      "repo": "Zorro909/weft-mods",
      "builds": [
        { "version": "0.2.0", "tag": "cogwork-v0.2.0", "abi": "f470b1cbb1f9ad91",
          "deps": ["base"], "sha256": "989b…" } ] }
  ],
  "revoked": [{ "sha256": "…", "reason": "Deletes saves" }]
}
```

- `weft` maps each Weft release tag to its mod ABI, newest first. The launcher takes the ABI of the player's Weft from it.
- `mods` is sorted by id, and builds are newest version first. A revoked build is left out.
- A build downloads from `https://github.com/<repo>/releases/download/<tag>/<id>.zip` and `<id>.mod.json`. The index holds no URLs.

## Trust model

1. A Weft mod is native code. It runs with the player's full privileges, and no badge changes that.
2. The index proves **which bytes were reviewed** and **which workflow built them**. It can't prove that a mod is harmless; that is what the human review is for.
3. **The sha256 is pinned by a reviewed PR.** CI doesn't take whatever a release holds today. A zip re-uploaded after review doesn't match the pin, so CI refuses it and the launcher won't install it.
4. **The build is attested.** The release's `<id>.mod.json` carries a GitHub build provenance attestation of the zip. CI checks it with `gh attestation verify --repo <repo>`, and the launcher checks it again itself, with an embedded Sigstore trust root:
   - the issuer is `https://token.actions.githubusercontent.com`;
   - the certificate is for a workflow of `https://github.com/<repo>/`;
   - the attested digest is the zip's sha256.

   So the reviewer knows which commit to read, and nobody could have uploaded a zip built on their own machine.
5. **The index isn't signed.** GitHub Pages serves it over HTTPS, built only from `main`, and `main` changes only through a reviewed PR (see MAINTAINING.md). The attestation ties a build to the repo the index names, and no more. So whoever can change the index could:
   - name another repo, and pin any build that repo's workflows made;
   - hide builds or revocations.

   What protects players is therefore this repository's branch protection, GitHub's HTTPS, and the review. Taking over a mod's repository or its maintainer's account can't change the pinned sha256: a new or re-uploaded build only reaches players through a new PR.
6. A revoked build is never installed, and the launcher turns it off where it is installed.
