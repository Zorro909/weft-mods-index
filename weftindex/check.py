"""`check`: online re-verification of what the index pins.

For each version that isn't revoked: the GitHub release still has the asset at the pinned URL,
GitHub's digest and the re-downloaded bytes both equal the pinned sha256 and size, the tag still
points at the pinned source commit, the zip holds the pinned platforms, the Weft commit still
hashes to the pinned ABI, and a pinned provenance still verifies. For weft.toml: each tag still
points at its commit, and the commit hashes to the ABI.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import model
from .net import GitHub, NotFound, sha256
from .release import (WEFT_REPO, ReleaseError, github_ahead_of, verify_attestation, weft_abi,
                      weft_tags, zip_platforms)


def changed_version_files(root: Path, base: str) -> set[Path]:
    """Version files added or changed since the merge base with `base`; all versions of a mod
    whose mod.toml changed."""
    out = subprocess.run(["git", "diff", "--name-only", "--diff-filter=AMR", f"{base}...HEAD"],
                         cwd=root, capture_output=True, text=True, check=True).stdout
    files = set()
    for name in out.split():
        p = root / name
        if p.match("mods/*/versions/*.toml"):
            files.add(p)
        elif p.match("mods/*/mod.toml"):
            files.update((p.parent / "versions").glob("*.toml"))
    return files


def check(gh: GitHub, root: Path, only: set[Path] | None = None, log=print) -> list[str]:
    repo = model.load_valid(root)
    errors: list[str] = []
    revoked = {r["sha256"] for r in repo.revoked}
    abi_cache: dict[str, str] = {}

    def abi_of(commit: str) -> str | None:
        if commit not in abi_cache:
            try:
                abi_cache[commit] = weft_abi(gh, commit)
            except ReleaseError as e:
                errors.append(str(e))
                abi_cache[commit] = None
        return abi_cache[commit]

    tags = weft_tags(gh)
    for rel in repo.weft:
        if tags.get(rel["tag"]) != rel["commit"]:
            errors.append(f"weft.toml: {WEFT_REPO} tag {rel['tag']} is at "
                          f"{tags.get(rel['tag'])}, not {rel['commit']}")
        abi = abi_of(rel["commit"])
        if abi is not None and abi != rel["abi"]:
            errors.append(f"weft.toml: {rel['tag']} hashes to ABI {abi}, not {rel['abi']}")
    log(f"weft.toml: {len(repo.weft)} releases checked")

    ahead_of = github_ahead_of(gh, WEFT_REPO)
    for mod_id, path, v in sorted(repo.versions(), key=lambda t: str(t[1])):
        if only is not None and path not in only:
            continue
        where = str(path.relative_to(root))
        if v["asset"]["sha256"] in revoked:
            log(f"{where}: revoked, skipped")
            continue
        before = len(errors)
        try:
            _check_version(gh, mod_id, v, where, errors, abi_of, ahead_of)
        except (ReleaseError, NotFound, RuntimeError) as e:
            errors.append(f"{where}: {e}")
        log(f"{where}: {'ok' if len(errors) == before else 'FAILED'}")
    return errors


def _check_version(gh, mod_id, v, where, errors, abi_of, ahead_of):
    src_repo, tag, asset = v["source"]["repo"], v["tag"], v["asset"]
    rel = gh.api(f"/repos/{src_repo}/releases/tags/{tag}")
    gh_asset = next((a for a in rel["assets"] if a["name"] == asset["name"]), None)
    if gh_asset is None or gh_asset["browser_download_url"] != asset["url"]:
        errors.append(f"{where}: release {tag} no longer has {asset['url']}")
        return
    if gh_asset.get("digest") != f"sha256:{asset['sha256']}":
        errors.append(f"{where}: GitHub digest is now {gh_asset.get('digest')}")
    data = gh.download(asset["url"])
    if sha256(data) != asset["sha256"] or len(data) != asset["size"]:
        errors.append(f"{where}: downloaded bytes have sha256 {sha256(data)} and size "
                      f"{len(data)}, not the pinned ones")
        return
    commit = gh.api(f"/repos/{src_repo}/commits/{tag}")["sha"]
    if commit != v["source"]["commit"]:
        errors.append(f"{where}: tag {tag} moved to {commit}")
    platforms = zip_platforms(data, mod_id)
    if platforms != v["platforms"]:
        errors.append(f"{where}: the zip holds {platforms}, not {v['platforms']}")
    weft = v["weft"]
    abi = abi_of(weft["commit"])
    if abi is not None and abi != weft["abi"]:
        errors.append(f"{where}: Weft {weft['commit'][:7]} hashes to ABI {abi}, not {weft['abi']}")
    m = model.DESCRIBE_RE.match(weft["tag"])
    if m and ahead_of(m.group(1), weft["commit"]) != int(m.group(2)):
        errors.append(f"{where}: weft.tag {weft['tag']} doesn't describe {weft['commit'][:7]}")
    p = v.get("provenance")
    if p is not None:
        got = verify_attestation(gh, src_repo, asset["sha256"], data, v["source"]["commit"],
                                 signer_workflow=p["workflow"])
        if got is None:
            errors.append(f"{where}: the pinned attestation is gone")
        elif (got["workflow"], got["ref"]) != (p["workflow"], p["ref"]):
            errors.append(f"{where}: attestation is by {got['workflow']}@{got['ref']}, "
                          f"pinned {p['workflow']}@{p['ref']}")
