# Maintaining the index

For reviewers and maintainers. Modders: see [README.md](README.md).

## Review

Approve a PR only after you have checked each of these:

- **The source.** The `verify` job's summary and the PR description list each build. They give the commit it was built from, as its attestation says, and the workflow that built it. Read the mod's code at that commit, at least what changed since the last reviewed version. Watch for:
  - network access;
  - process spawning;
  - file access outside the game's folders;
  - obfuscated or vendored binaries.
- **The workflow.** It builds from source, in the mod's own repository.
- **The text.** The name and summary are fine to show to players, and the deps look right.
- **The diff.** It touches only `mods/`, unless it is meant to change the tooling. A change to `weftindex/` or `.github/` is code that decides what gets published: review it as such, in a PR of its own.
- **CI is green.**

Merging to `main` publishes.

## How it works

```
 PR ──► Check:   test (Python 3.11 and latest): unit tests, validate
                 verify: build (downloads and verifies every release)
 merge to main ──► Publish: unit tests, build ──► GitHub Pages (v2/index.json)
```

- `weftindex/model.py` holds the files and their rules (offline), and renders `index.json`.
- `weftindex/release.py`:
  - `inspect` downloads one release and checks it: the pinned sha256, the attestation in `<id>.mod.json`, the zip's contents and its `weft-mod.toml`;
  - `build` runs `inspect` for every version that isn't revoked;
  - `add-release` writes the files of new releases.
- `weftindex/weft.py` keeps `weft.toml`, and `build` re-checks it: each tag still points at its commit, and the commit's ABI file still hashes to its ABI.
- `weftindex/net.py` does all GitHub access, and runs `gh attestation verify`. The tests replace it with a fake (`tests/fixtures.py`).

The output is deterministic: mods sorted by id, builds newest version first, `weft` newest release first.

## Revoking a build

Add an entry to `revoked.toml` and merge it:

```toml
[[revoked]]
sha256 = "989b21912db1c53d06460cf7bbf9a979ce461c3fe197215124851a427bf7d20c"
reason = "Shipped a debug build that deletes saves"   # optional; the player sees it
```

Keep the version file. `build` skips revoked versions without downloading them, and leaves them out of `mods`.

## When Publish fails

`build` refuses to publish if any release fails to verify. Pages then keeps serving the last index, which also holds back revocations merged since. Fix it fast:

- **A release that changed or disappeared upstream:** revoke its sha256, or delete its version file, and merge.
- **A moved or deleted Weft tag** (a `weft.toml` error): a Weft release tag must never move. If Weft's maintainers can't put it back, fix or delete its entry in `weft.toml`.
- **A transient GitHub error:** re-run the workflow. `net.py` already retries server errors and dropped connections three times.

## New Weft releases

When Weft makes a release, run `python3 -m weftindex sync-weft` and open a PR. The launcher learns each release's ABI from the index's `weft` map.

## Repository settings

The trust model depends on settings that aren't in the repository:

- **Branch protection on `main`.** Require a PR, one approving review from someone with write access, and the status checks `test (3.11)`, `test (3.x)` and `verify`. GitHub doesn't let you approve your own PR. A single maintainer therefore merges their own PRs as an admin, once the checks are green.
- **Pages:** the source is GitHub Actions.
- **Actions** are pinned by commit. Dependabot ([`.github/dependabot.yml`](.github/dependabot.yml)) opens a PR for each update.

## Tooling

```
python3 -m weftindex add-release <repo> <tag>... [--summary S]   verify releases, write their files (online)
python3 -m weftindex validate                                    check every file (offline)
python3 -m weftindex build [--out dist]                          verify every release, write <out>/v2/index.json (online)
python3 -m weftindex sync-weft                                   add every new Weft release to weft.toml (online)
python3 -m unittest discover -s tests -t .                       unit tests
```

The online commands need a GitHub token (`GH_TOKEN`, or a logged-in `gh`). `add-release` and `build` also need `gh` 2.49 or newer.
