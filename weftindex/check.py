"""`check`: online re-verification of what the index pins.

Each version file that isn't revoked is inspected again, exactly as `add-release` inspected it
(`release.inspect`), and every field must still be the same: the asset's bytes, GitHub's digest,
the tag's commit, the platforms, the deps, the Weft commit and its ABI, and the provenance. A
release that has an attestation now, but not when it was added, fails too. For weft.toml: each tag
still points at its commit, and the commit hashes to the ABI.

With `--since` (on a pull request), only the version files the PR adds or changes are checked.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import model
from .net import GitHub
from .release import inspect
from .weft import WEFT_REPO, Weft

# When the attestation was verified, not what it says. `reviewed` isn't compared either, since it
# isn't in what a release proves: `differences` compares only the top-level keys of `now`.
NOT_COMPARED = {"provenance.verified_at"}


def changed_version_files(root: Path, base: str) -> set[Path]:
    """Version files added or changed since the merge base with `base`. (A changed mod.toml needs
    no online check: `validate` covers it.)"""
    r = subprocess.run(["git", "diff", "--name-only", "--diff-filter=AMR", f"{base}...HEAD"],
                       cwd=root, capture_output=True, text=True)
    if r.returncode != 0:
        raise model.Error(f"git diff against {base} failed: {r.stderr.strip()}")
    files = set()
    for name in r.stdout.split():
        if (root / name).match("mods/*/versions/*.toml"):
            files.add(root / name)
    return files


def _flat(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(_flat(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def differences(pinned: dict, now: dict) -> list[str]:
    """What differs between a version file and what its release proves now, one line each."""
    a = _flat({"provenance": None, **pinned})
    b = _flat(now)
    out = []
    for key in sorted(set(a) | set(b)):
        if key.split(".")[0] in now and key not in NOT_COMPARED and a.get(key) != b.get(key):
            out.append(f"{key} is pinned as {a.get(key)!r}, but the release now gives {b.get(key)!r}")
    return out


def check(gh: GitHub, root: Path, only: set[Path] | None = None, log=print) -> list[str]:
    repo = model.load_valid(root)
    weft = Weft(gh)
    errors: list[str] = []

    for rel in repo.weft:
        try:
            now = weft.release(rel["tag"]) if rel["tag"] in weft.tags else None
        except model.Error as e:
            errors.append(f"weft.toml: {rel['tag']}: {e}")
            continue
        if now is None:
            errors.append(f"weft.toml: {WEFT_REPO} has no tag {rel['tag']}")
        else:
            errors += [f"weft.toml: {rel['tag']}: {d}" for d in differences(rel, now)]
    log(f"weft.toml: {len(repo.weft)} releases checked")

    revoked = {r["sha256"] for r in repo.revoked}
    for _, path, v in sorted(repo.versions(), key=lambda t: str(t[1])):
        if only is not None and path not in only:
            continue
        where = str(path.relative_to(root))
        if v["asset"]["sha256"] in revoked:
            log(f"{where}: revoked, skipped")
            continue
        try:
            found = differences(v, inspect(gh, weft, v["source"]["repo"], v["tag"], v).entry)
        except model.Error as e:
            found = [str(e)]
        errors += [f"{where}: {d}" for d in found]
        log(f"{where}: {'FAILED' if found else 'ok'}")
    return errors
