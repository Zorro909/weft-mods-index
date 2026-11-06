# Maintaining the index

For reviewers and maintainers. Modders: see [README.md](README.md).

## Review

Approve a PR only after you have checked each of these:

- **The source.** Read the mod's code at `source.commit`, at least what changed since the last reviewed version. The PR description that `add-release` writes links both. Watch for:
  - network access;
  - process spawning;
  - file access outside the game's folders;
  - obfuscated or vendored binaries.
- **The build.**
  - With `[provenance]`, the attestation proves which workflow built the zip, and that it built `source.commit` or its parent. Check that the workflow builds from source, and that the parent, if that's what was built, only differs by a version bump.
  - Without `[provenance]`, you are trusting the release author's build.
- **The metadata.** The name, summary and icon are fine to show to players, and `deps` and `platforms` look right.
- **The files are generated.** Only `mod.toml`'s wording may be edited by hand. `Check` rejects a version file that its release and `<id>.mod.json` don't reproduce, field for field (`reviewed` aside).
- **`[reviewed]`.** `add-release` fills it in with the login of whoever ran it. When you approve, set `by` to your GitHub login and `at` to today in each version file; a suggested change in the PR is enough.
- **CI is green.**

Merging to `main` publishes.

To add a release yourself, run `add-release` as in the README. It fills in `[reviewed]` with your `gh` login.

## How it works

```
 PR ──► Check workflow:
        test (Python 3.11 and latest): unit tests ─► validate
        verify (main's tooling): validate ─► check --since (re-verifies the changed version files)
 merge to main, and every Monday ──► Publish workflow:
        verify: unit tests ─► check (re-verifies everything)
        sign (holds the key, offline only): build v1/index.json ─► minisign ─► GitHub Pages
```

- `verify` runs `main`'s copy of the tooling, so a PR can't make itself pass by changing a check. A PR that changes the tooling and adds version files at once fails it; split it into two PRs.

- `weftindex/model.py` loads and validates the files offline, and renders `index.json`. `VERSION_FIELDS` and `MOD_FIELDS` are the schema; `validate` is the rules, and the tests in `tests/test_model.py` show each one.
- `weftindex/release.py` has `inspect`, which reads a GitHub release and cross-checks it, and `add-release`, which writes what `inspect` returns.
- `weftindex/weft.py` knows Weft's tags and ABIs, and whether a `weft.tag` names its commit; `weftindex/platforms.py` reads the library headers in a zip.
- `weftindex/check.py` runs `inspect` again for every pinned version and reports each field that differs. A pinned `weft.tag` only has to still be true, so a later Weft tag doesn't break an existing entry.
- `weftindex/net.py` does all GitHub access. `tests/test_net.py` tests it with HTTP and `gh` mocked; the other online tests replace it with a fake (`tests/test_online.py`).

The launcher's contract for `index.json` is `docs/mod-manager/INDEX_FORMAT.md` in Weft Launcher. The output is deterministic:

- mods are sorted by id;
- versions are newest first;
- `weft[]` is newest release first;
- `generated` is the time of the built commit, so a rebuild gives the same bytes.

## How the launcher verifies

1. It verifies `index.json.minisig` over the exact bytes of `index.json` with its embedded key. If that fails, it keeps the last verified index.
2. It refuses an index older than the newest one it has accepted, by `generated` and by the signature's trusted `timestamp:`. It calls an index signed more than 30 days ago stale; the weekly publish re-signs, so a current index never is.
3. It offers only versions whose `weft.abi` equals the ABI of the player's Weft (`weft[]` maps releases to ABIs).
4. It downloads `asset.url`, which must be under `https://github.com/<source.repo>/releases/download/`, and installs it only if its sha256 equals `asset.sha256`.
5. It never installs a sha256 listed in `revoked`, and turns installed copies off.

## Revoking a build

Add an entry to `revoked.toml` and merge it:

```toml
[[revoked]]
sha256 = "989b21912db1c53d06460cf7bbf9a979ce461c3fe197215124851a427bf7d20c"
reason = "Shipped a debug build that deletes saves"
at = "2026-11-05"
```

Keep the version file, so the launcher can tell players which mod was revoked. `check` skips revoked versions.

## When the weekly publish fails

Publish re-verifies every pinned release before it signs. If one fails, nothing is published until it is fixed. That includes a revocation merged in the meantime, and the weekly re-sign. The launcher calls the index stale after 30 days.

The way out is almost always to revoke:

- **An asset, tag or attestation that changed or disappeared upstream:** treat the release as untrusted. Revoke its sha256, or delete its version file if it was never in a published index, and merge. `check` skips revoked versions.
- **A moved Weft tag** (a `weft.toml` error): a Weft release tag must never move, because version files pin its commit. If Weft's maintainers can't put it back, revoke every version whose `weft.tag` is or is based on that tag, and delete the tag's entry from `weft.toml`. A revoked version may name a Weft release that `weft.toml` no longer lists.
- **A transient GitHub error:** re-run the workflow. `net.py` already retries server errors and dropped connections three times.

## New Weft releases

When Weft makes a release, run `python3 -m weftindex sync-weft` and open a PR. The launcher learns each release's ABI from `weft.toml`, and modders' PRs then don't need to touch it. (`add-release` still adds a release a mod needs, if it's missing.)

## Repository settings

The trust model depends on settings that aren't in the repository:

- **Branch protection on `main`:** require a PR, one approving review from someone with write access, and the status checks `test (3.11)`, `test (3.x)` and `verify`. GitHub doesn't let you approve your own PR, so with a single maintainer, that maintainer merges their own PRs as an admin, after the checks are green.
- **The `index-signing` environment:** it holds the secrets `MINISIGN_SECRET_KEY` and `MINISIGN_PASSWORD`, and its deployment policy allows only `main`.
- **Pages:** the source is GitHub Actions.
- **Actions** are pinned by commit. Dependabot ([`.github/dependabot.yml`](.github/dependabot.yml)) opens a PR for each update; review it like code that runs next to the signing key.

## Rotating the signing key

Only the key embedded in the launcher matters. A rotation therefore needs a launcher release, and the old key keeps signing until most players have that release.

1. Make a new key, with a strong password: `minisign -G -p keys/index.pub.new -s index.key`.
2. Ship a launcher release that accepts both the old and the new public key.
3. In one go, replace the secrets and the public key. Publishing fails between the two, because CI checks the signature against `keys/index.pub`.

   ```sh
   gh secret set MINISIGN_SECRET_KEY --env index-signing < index.key
   gh secret set MINISIGN_PASSWORD --env index-signing
   git mv -f keys/index.pub.new keys/index.pub   # then commit, PR and merge right away
   ```

4. In a later launcher release, drop the old key.

If the key leaks:

1. Do steps 1 and 3 at once.
2. Ship a launcher release with only the new key.
3. Until players update, an attacker with the old key can sign any index, with any sha256 values and any source repositories. The launcher still downloads only from GitHub release URLs, but that is all that holds. Act fast.

## Tooling

```
python3 -m weftindex add-release <repo> <tag>...           write version files (online)
python3 -m weftindex validate                              validate every file (offline)
python3 -m weftindex build [--out dist] [--generated T]    validate and write <out>/v1/index.json
python3 -m weftindex check [--since <ref>]                 re-verify pinned releases (online)
python3 -m weftindex sync-weft                             add every new Weft release to weft.toml
python3 -m unittest discover -s tests -t .                 unit tests
```

The online commands need a GitHub token: `GH_TOKEN`, or a logged-in `gh`.
