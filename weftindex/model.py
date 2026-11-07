"""The files of the index repository, their rules, and how `v2/index.json` is rendered from them.

    weft.toml                          Weft releases and their ABI           -> index `weft`
    revoked.toml                       revoked zips, by sha256               -> index `revoked`
    mods/<id>/mod.toml                 summary, and optionally name          -> index `mods[]`
    mods/<id>/versions/<version>.toml  repo, tag, sha256 of one release      -> `mods[].builds[]`

Everything here is offline. `build.py` downloads each release and reads its `weft-mod.toml`.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = 2

# The launcher's rules (Weft Launcher, the mod manager's index format); it skips entries that
# break them, so the index refuses them.
ID_RE = re.compile(r"[a-z0-9_]{1,64}")
REPO_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")
TAG_RE = re.compile(r"(?!\.)[A-Za-z0-9._-]{1,100}")
ABI_RE = re.compile(r"[0-9a-f]{16}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
# SemVer 2.0 (https://semver.org) without build metadata, which a tag can't hold.
_PRE_ID = r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
SEMVER_RE = re.compile(rf"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-({_PRE_ID}(?:\.{_PRE_ID})*))?")
# Ours: weft.toml.
WEFT_TAG_RE = re.compile(r"v[0-9A-Za-z.\-]+")
SHA1_RE = re.compile(r"[0-9a-f]{40}")

SUMMARY_MAX = 140
NAME_MAX = 64
BASE = "base"  # the mod that ships with the game: always a valid dep

# The keys of each file: True if required.
MOD_KEYS = {"summary": True, "name": False}
VERSION_KEYS = {"repo": True, "tag": True, "sha256": True}
MANIFEST_KEYS = {"id": True, "name": True, "version": True, "abi": True, "deps": True}
WEFT_KEYS = {"tag": True, "commit": True, "abi": True}
REVOKED_KEYS = {"sha256": True, "reason": False}


class Error(Exception):
    """An expected failure: the CLI prints it as one line, without a traceback."""


class ValidationError(Error):
    def __init__(self, errors: list[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


@dataclass
class Mod:
    meta: dict  # mod.toml
    versions: dict[str, dict]  # version (the file name) -> version file


@dataclass
class Repo:
    weft: list[dict] = field(default_factory=list)
    revoked: list[dict] = field(default_factory=list)
    mods: dict[str, Mod] = field(default_factory=dict)

    def revoked_shas(self) -> set[str]:
        return {r["sha256"] for r in self.revoked}


def zip_url(repo: str, tag: str, mod_id: str, suffix: str = ".zip") -> str:
    return f"https://github.com/{repo}/releases/download/{tag}/{mod_id}{suffix}"


def semver_key(version: str) -> tuple:
    """A sort key in SemVer precedence order."""
    m = SEMVER_RE.fullmatch(version)
    if not m:
        return ((), ())
    core = tuple(int(m.group(i)) for i in (1, 2, 3))
    if m.group(4) is None:
        return (core, (1,))
    return (core, (0,) + tuple((0, int(p), "") if p.isdigit() else (1, 0, p)
                               for p in m.group(4).split(".")))


def weft_tag_key(tag: str) -> tuple:
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"[.\-]", tag[1:]))


def one_line(text: str) -> bool:
    return not any(ord(c) < 0x20 or ord(c) == 0x7F or c in "  " for c in text)


# ---------------------------------------------------------------------------------------------
# Loading and validation


def _load_toml(path: Path, root: Path, errors: list[str]) -> dict | None:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except (OSError, ValueError) as e:  # ValueError: bad TOML, or not UTF-8
        errors.append(f"{path.relative_to(root)}: {e}")
        return None


def check_keys(data, spec: dict[str, bool], where: str, errors: list[str]) -> bool:
    if not isinstance(data, dict):
        errors.append(f"{where}: must be a table")
        return False
    n = len(errors)
    errors += [f"{where}: unknown key {k}" for k in sorted(set(data) - set(spec))]
    errors += [f"{where}: missing key {k}" for k, req in spec.items() if req and k not in data]
    return len(errors) == n


def check_string(value, pattern: re.Pattern | None, what: str, where: str,
                 errors: list[str]) -> bool:
    if not isinstance(value, str) or not value:
        errors.append(f"{where}: {what} must be a non-empty string")
    elif pattern is not None and not pattern.fullmatch(value):
        errors.append(f"{where}: {what} {value!r} does not match {pattern.pattern}")
    else:
        return True
    return False


def check_text(value, what: str, limit: int, where: str, errors: list[str]) -> None:
    if check_string(value, None, what, where, errors):
        if not one_line(value):
            errors.append(f"{where}: {what} must be one line")
        elif len(value) > limit:
            errors.append(f"{where}: {what} is {len(value)} characters, more than {limit}")


def load(root: Path) -> tuple[Repo, list[str]]:
    """The repository's files, and every error found in them."""
    errors: list[str] = []
    repo = Repo()
    for name, key, spec in (("weft.toml", "release", WEFT_KEYS),
                            ("revoked.toml", "revoked", REVOKED_KEYS)):
        data = _load_toml(root / name, root, errors) or {}
        check_keys(data, {key: False}, name, errors)
        entries = data.get(key, [])
        if not isinstance(entries, list):
            errors.append(f"{name}: {key} must be an array of tables")
            entries = []
        good = [e for i, e in enumerate(entries)
                if check_keys(e, spec, f"{name} {key} #{i + 1}", errors)]
        setattr(repo, "weft" if key == "release" else "revoked", good)
    _check_weft(repo.weft, errors)
    _check_revoked(repo.revoked, errors)

    for mod_dir in sorted(p for p in (root / "mods").glob("*") if p.is_dir()):
        mod_id, where = mod_dir.name, str(mod_dir.relative_to(root))
        check_string(mod_id, ID_RE, "the mod id (folder name)", where, errors)
        stray = sorted(p.name for p in mod_dir.iterdir() if p.name not in ("mod.toml", "versions"))
        if stray:
            errors.append(f"{where}: unexpected files {stray}")
        meta = _load_toml(mod_dir / "mod.toml", root, errors)
        if meta is not None and check_keys(meta, MOD_KEYS, f"{where}/mod.toml", errors):
            check_text(meta["summary"], "summary", SUMMARY_MAX, f"{where}/mod.toml", errors)
            if "name" in meta:
                check_text(meta["name"], "name", NAME_MAX, f"{where}/mod.toml", errors)
        versions = {}
        for vf in sorted((mod_dir / "versions").glob("*")):
            v = _load_toml(vf, root, errors) if vf.suffix == ".toml" else None
            if vf.suffix != ".toml":
                errors.append(f"{vf.relative_to(root)}: not a .toml file")
            elif v is not None and _check_version(mod_id, vf.stem, v, str(vf.relative_to(root)),
                                                   errors):
                versions[vf.stem] = v
        if not versions:
            errors.append(f"{where}: no versions")
        repos = {v["repo"] for v in versions.values()}
        if len(repos) > 1:
            errors.append(f"{where}: one mod, one repo; found {sorted(repos)}")
        if meta is not None:
            repo.mods[mod_id] = Mod(meta, versions)

    seen: dict[str, str] = {}
    for mod_id, mod in repo.mods.items():
        for version, v in mod.versions.items():
            where = f"mods/{mod_id}/versions/{version}.toml"
            if v["sha256"] in seen:
                errors.append(f"{where}: sha256 is also pinned by {seen[v['sha256']]}")
            seen.setdefault(v["sha256"], where)
    return repo, errors


def _check_version(mod_id: str, version: str, v: dict, where: str, errors: list[str]) -> bool:
    n = len(errors)
    if check_keys(v, VERSION_KEYS, where, errors):
        check_string(version, SEMVER_RE, "the version (file name)", where, errors)
        check_string(v["repo"], REPO_RE, "repo", where, errors)
        if check_string(v["tag"], TAG_RE, "tag", where, errors) and v["tag"] != f"{mod_id}-v{version}":
            errors.append(f"{where}: tag must be {mod_id}-v{version}")
        check_string(v["sha256"], SHA256_RE, "sha256", where, errors)
    return len(errors) == n


def _check_weft(releases: list[dict], errors: list[str]) -> None:
    tags = set()
    for i, rel in enumerate(releases):
        where = f"weft.toml release #{i + 1}"
        check_string(rel["tag"], WEFT_TAG_RE, "tag", where, errors)
        check_string(rel["commit"], SHA1_RE, "commit", where, errors)
        check_string(rel["abi"], ABI_RE, "abi", where, errors)
        if rel["tag"] in tags:
            errors.append(f"{where}: duplicate tag {rel['tag']}")
        tags.add(rel["tag"])


def _check_revoked(revoked: list[dict], errors: list[str]) -> None:
    shas = set()
    for i, rev in enumerate(revoked):
        where = f"revoked.toml revoked #{i + 1}"
        if check_string(rev["sha256"], SHA256_RE, "sha256", where, errors):
            if rev["sha256"] in shas:
                errors.append(f"{where}: duplicate sha256 {rev['sha256']}")
            shas.add(rev["sha256"])
        if "reason" in rev:
            check_text(rev["reason"], "reason", SUMMARY_MAX, where, errors)


def load_valid(root: Path) -> Repo:
    repo, errors = load(root)
    if errors:
        raise ValidationError(errors)
    return repo


def check_manifest(mod_id: str, version: str, manifest: dict, where: str) -> list[str]:
    """The errors of a release's `weft-mod.toml`, for the version file `version` of `mod_id`."""
    errors: list[str] = []
    if not check_keys(manifest, MANIFEST_KEYS, where, errors):
        return errors
    if manifest["id"] != mod_id:
        errors.append(f"{where}: id is {manifest['id']!r}, not {mod_id!r}")
    if manifest["version"] != version:
        errors.append(f"{where}: version is {manifest['version']!r}, not {version!r}")
    check_text(manifest["name"], "name", NAME_MAX, where, errors)
    check_string(manifest["abi"], ABI_RE, "abi", where, errors)
    deps = manifest["deps"]
    if not isinstance(deps, list) or not all(isinstance(d, str) and ID_RE.fullmatch(d)
                                             for d in deps):
        errors.append(f"{where}: deps must be a list of mod ids")
    elif len(set(deps)) != len(deps) or mod_id in deps:
        errors.append(f"{where}: deps must be unique and must not include {mod_id}")
    return errors


# ---------------------------------------------------------------------------------------------
# Rendering


def render(repo: Repo, manifests: dict[tuple[str, str], dict]) -> dict:
    """`v2/index.json`, from the repository and the `weft-mod.toml` of each build it lists
    (by mod id and version). A version without a manifest, a revoked one, is left out."""
    mods = []
    for mod_id in sorted(repo.mods):
        mod = repo.mods[mod_id]
        versions = sorted((v for v in mod.versions if (mod_id, v) in manifests),
                          key=semver_key, reverse=True)
        if not versions:
            continue
        builds = []
        for version in versions:
            m, v = manifests[mod_id, version], mod.versions[version]
            builds.append({"version": version, "tag": v["tag"], "abi": m["abi"],
                           "deps": list(m["deps"]), "sha256": v["sha256"]})
        mods.append({
            "id": mod_id,
            "name": mod.meta.get("name") or manifests[mod_id, versions[0]]["name"],
            "summary": mod.meta["summary"],
            "repo": mod.versions[versions[0]]["repo"],
            "builds": builds,
        })
    weft = sorted(repo.weft, key=lambda r: weft_tag_key(r["tag"]), reverse=True)
    revoked = sorted(repo.revoked, key=lambda r: r["sha256"])
    return {
        "schema": SCHEMA,
        "weft": {r["tag"]: r["abi"] for r in weft},
        "mods": mods,
        "revoked": [{k: r[k] for k in REVOKED_KEYS if k in r} for r in revoked],
    }


def dumps(index: dict) -> bytes:
    """The exact bytes of `index.json`: two-space indent, UTF-8, LF, trailing newline."""
    return (json.dumps(index, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
