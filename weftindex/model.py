"""Loads the reviewed entries from the repository, validates them against the index contract
(Weft Launcher's `docs/mod-manager/INDEX_FORMAT.md`), and renders `v1/index.json`.

Layout of the source of truth:

    weft.toml                          Weft releases and their ABI hash  -> index `weft[]`
    revoked.toml                       revoked sha256 values             -> index `revoked[]`
    mods/<id>/mod.toml                 the mod's name, summary, ...      -> index `mods[]`
    mods/<id>/versions/<version>.toml  one reviewed release              -> `mods[].versions[]`

Everything here is offline: no network access. `check.py` does the online re-verification.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = 1

ID_RE = re.compile(r"^[a-z0-9_]+$")
WEFT_TAG_RE = re.compile(r"^v[0-9A-Za-z.\-]+$")
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ABI_RE = re.compile(r"^[0-9a-f]{16}$")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"  # RFC 3339, UTC
DATE_FORMAT = "%Y-%m-%d"
REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
# SemVer 2.0 (https://semver.org), without build metadata: `+` can't be in a tag URL unescaped.
# One exception: `-experimental.<weft7>.<mod7>`, whose sha7s may be all digits with a leading zero.
_PRE_ID = r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    rf"(?:-({_PRE_ID}(?:\.{_PRE_ID})*|experimental\.[0-9a-f]{{7}}\.[0-9a-f]{{7}}))?$"
)
# An SPDX license expression made of ids, `AND`, `OR`, `WITH` and parentheses.
SPDX_RE = re.compile(r"^[A-Za-z0-9.\-+()]+(?:\s+(?:AND|OR|WITH)\s+[A-Za-z0-9.\-+()]+)*$")
WORKFLOW_RE = re.compile(r"^\.github/workflows/[A-Za-z0-9._-]+\.ya?ml$")
GIT_REF_RE = re.compile(r"^refs/(?:heads|tags)/[A-Za-z0-9._/\-]+$")

# The order platforms are listed in.
PLATFORMS = ("windows-x64", "linux-x64", "macos-arm64")
SUMMARY_MAX = 140
# Experimental builds carry `-experimental.<weft7>.<mod7>` in their tag, and since a version
# file's version is its tag without `<id>-v`, in their version too.
EXPERIMENTAL_RE = re.compile(r"-experimental\.[0-9a-f]{7}\.[0-9a-f]{7}$")
# `git describe --tags` of a Weft commit that is not itself a release: `<tag>-<n>-g<sha7>`.
DESCRIBE_RE = re.compile(r"^(v[0-9A-Za-z.\-]+?)-(\d+)-g([0-9a-f]{7})$")

# The fields of mod.toml, in index.json order, and whether each is required.
MOD_FIELDS = {"name": True, "summary": True, "authors": True, "license": False,
              "homepage": False, "icon": False}
# The fields of a version file, in index.json order: a table's keys, or None for a plain value.
# All are required but `provenance`. tomlw writes the plain values first, then the tables.
VERSION_FIELDS = {
    "version": None, "channel": None, "tag": None, "published": None,
    "source": ("repo", "commit"),
    "weft": ("tag", "commit", "abi"),
    "deps": None, "platforms": None,
    "asset": ("name", "url", "size", "sha256"),
    "provenance": ("kind", "repo", "workflow", "ref", "verified_at"),
    "reviewed": ("by", "at"),
}
OPTIONAL = {"provenance"}


class Error(Exception):
    """An expected failure: the CLI prints it as one line, without a traceback."""


class ValidationError(Error):
    def __init__(self, errors: list[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


@dataclass
class Mod:
    meta: dict  # mod.toml
    versions: list[tuple[Path, dict]]  # (path, version file)


@dataclass
class Repo:
    """The parsed source of truth, before validation."""
    root: Path
    weft: list[dict] = field(default_factory=list)
    revoked: list[dict] = field(default_factory=list)
    mods: dict[str, Mod] = field(default_factory=dict)

    def versions(self):
        """Every (mod id, path, version file)."""
        for mod_id, mod in self.mods.items():
            for path, v in mod.versions:
                yield mod_id, path, v


def expected_asset_url(repo: str, tag: str, mod_id: str) -> str:
    return f"https://github.com/{repo}/releases/download/{tag}/{mod_id}.zip"


def tag_version(mod_id: str, tag: str) -> str | None:
    prefix = f"{mod_id}-v"
    return tag[len(prefix):] if tag.startswith(prefix) else None


def split_tag(tag: str) -> tuple[str, str] | None:
    """`cogwork-v0.2.0` -> (`cogwork`, `0.2.0`)."""
    m = re.match(r"^([a-z0-9_]+)-v(\d.*)$", tag)
    return (m.group(1), m.group(2)) if m else None


def channel_for(version: str) -> str:
    """The channel of a version: `experimental` for an experimental build, `prerelease` for any
    other SemVer prerelease, else `release`. It depends on the version alone, so editing the
    release on GitHub later can't change it."""
    if EXPERIMENTAL_RE.search(version):
        return "experimental"
    return "prerelease" if "-" in version else "release"


def weft_base(name: str) -> str:
    """The release tag a Weft name is based on: `v26.11.1-46-g4563878` -> `v26.11.1`."""
    m = DESCRIBE_RE.fullmatch(name)
    return m.group(1) if m else name


def semver_key(version: str) -> tuple:
    """A sort key in SemVer precedence order."""
    m = SEMVER_RE.match(version)
    if not m:
        return ((), ())
    core = tuple(int(m.group(i)) for i in (1, 2, 3))
    pre = m.group(4)
    if pre is None:
        return (core, (1,))
    parts = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
    return (core, (0,) + parts)


def weft_tag_key(tag: str) -> tuple:
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"[.\-]", tag[1:]))


# ---------------------------------------------------------------------------------------------
# Loading


def _load_toml(path: Path, errors: list[str]) -> dict | None:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except (OSError, ValueError) as e:  # ValueError: bad TOML, or not UTF-8
        errors.append(f"{path}: {e}")
        return None


def load(root: Path) -> tuple[Repo, list[str]]:
    errors: list[str] = []
    repo = Repo(root=root)
    for name, key in (("weft.toml", "release"), ("revoked.toml", "revoked")):
        data = _load_toml(root / name, errors)
        if data is None:
            continue
        unknown = set(data) - {key}
        if unknown:
            errors.append(f"{name}: unknown keys {sorted(unknown)}")
        entries = data.get(key, [])
        if not isinstance(entries, list):
            errors.append(f"{name}: {key} must be an array of tables")
            entries = []
        setattr(repo, "weft" if key == "release" else "revoked", entries)
    mods_dir = root / "mods"  # absent while the index has no mods
    for mod_dir in sorted(p for p in mods_dir.glob("*") if p.is_dir()):
        if not (mod_dir / "mod.toml").exists():
            errors.append(f"{mod_dir.relative_to(root)}: no mod.toml")
            continue
        meta = _load_toml(mod_dir / "mod.toml", errors)
        if meta is None:
            continue
        versions = []
        for vf in sorted((mod_dir / "versions").glob("*")):
            if vf.suffix != ".toml" or not vf.is_file():
                errors.append(f"{vf.relative_to(root)}: not a .toml version file")
                continue
            data = _load_toml(vf, errors)
            if data is not None:
                versions.append((vf, data))
        stray = [p.name for p in mod_dir.iterdir() if p.name not in ("mod.toml", "versions")]
        if stray:
            errors.append(f"{mod_dir.relative_to(root)}: unexpected files {sorted(stray)}")
        repo.mods[mod_dir.name] = Mod(meta, versions)
    return repo, errors


# ---------------------------------------------------------------------------------------------
# Validation


class _Checker:
    def __init__(self, where: str, errors: list[str]):
        self.where = where
        self.errors = errors

    def err(self, msg: str) -> None:
        self.errors.append(f"{self.where}: {msg}")

    def keys(self, data, spec: dict[str, bool], what: str = "") -> bool:
        if not isinstance(data, dict):
            self.err(f"{what or 'entry'} must be a table")
            return False
        prefix = f"{what}." if what else ""
        ok = True
        for k in sorted(set(data) - set(spec)):
            self.err(f"unknown key {prefix}{k}")
            ok = False
        for k, required in spec.items():
            if required and k not in data:
                self.err(f"missing key {prefix}{k}")
                ok = False
        return ok

    def string(self, value, what: str, pattern: re.Pattern | None = None) -> bool:
        if not isinstance(value, str) or not value:
            self.err(f"{what} must be a non-empty string")
            return False
        if pattern is not None and not pattern.fullmatch(value):
            self.err(f"{what} {value!r} does not match {pattern.pattern}")
            return False
        return True

    def time(self, value, what: str, fmt: str = TIME_FORMAT) -> bool:
        """A date or time in `fmt` that exists on the calendar."""
        if not self.string(value, what):
            return False
        try:
            ok = dt.datetime.strptime(value, fmt).strftime(fmt) == value
        except ValueError:
            ok = False
        if not ok:
            self.err(f"{what} {value!r} is not a {fmt} date")
        return ok

    def https_url(self, value, what: str, suffix: str | None = None) -> bool:
        if not self.string(value, what):
            return False
        if not value.startswith("https://") or any(c.isspace() for c in value):
            self.err(f"{what} must be an https URL")
            return False
        if suffix and not value.lower().endswith(suffix):
            self.err(f"{what} must end in {suffix}")
            return False
        return True


def one_line(text: str) -> bool:
    return not any(ord(c) < 0x20 or ord(c) == 0x7F or c in "  " for c in text)


def validate(repo: Repo) -> list[str]:
    errors: list[str] = []

    weft_by_tag: dict[str, dict] = {}
    weft_commits: dict[str, str] = {}
    for i, rel in enumerate(repo.weft):
        c = _Checker(f"weft.toml release #{i + 1}", errors)
        if not c.keys(rel, {"tag": True, "commit": True, "abi": True}):
            continue
        ok = c.string(rel["tag"], "tag", WEFT_TAG_RE)
        ok &= c.string(rel["commit"], "commit", SHA1_RE)
        ok &= c.string(rel["abi"], "abi", ABI_RE)
        if not ok:
            continue
        if rel["tag"] in weft_by_tag:
            c.err(f"duplicate tag {rel['tag']}")
        weft_by_tag[rel["tag"]] = rel
        weft_commits.setdefault(rel["commit"], rel["abi"])
        if weft_commits[rel["commit"]] != rel["abi"]:
            c.err(f"abi differs from another tag of commit {rel['commit']}")

    revoked_shas = set()
    for i, rev in enumerate(repo.revoked):
        c = _Checker(f"revoked.toml entry #{i + 1}", errors)
        if not c.keys(rev, {"sha256": True, "reason": True, "at": True}):
            continue
        if c.string(rev["sha256"], "sha256", SHA256_RE):
            if rev["sha256"] in revoked_shas:
                c.err(f"duplicate sha256 {rev['sha256']}")
            revoked_shas.add(rev["sha256"])
        if c.string(rev["reason"], "reason") and not one_line(rev["reason"]):
            c.err("reason must be one line")
        c.time(rev["at"], "at", DATE_FORMAT)

    seen = _Seen(weft_by_tag, {commit: (abi, "weft.toml") for commit, abi in weft_commits.items()},
                 revoked_shas)
    for mod_id, mod in repo.mods.items():
        meta, versions = mod.meta, mod.versions
        c = _Checker(f"mods/{mod_id}/mod.toml", errors)
        if not ID_RE.match(mod_id):
            c.err(f"mod id {mod_id!r} (the folder name) does not match {ID_RE.pattern}")
        if c.keys(meta, MOD_FIELDS):
            c.string(meta["name"], "name")
            if c.string(meta["summary"], "summary"):
                s = meta["summary"]
                if not one_line(s):
                    c.err("summary must be one line")
                if len(s) > SUMMARY_MAX:
                    c.err(f"summary is {len(s)} characters, more than {SUMMARY_MAX}")
            authors = meta["authors"]
            if not isinstance(authors, list) or not all(isinstance(a, str) and a for a in authors):
                c.err("authors must be a list of non-empty strings")
            if "license" in meta:
                c.string(meta["license"], "license", SPDX_RE)
            if "homepage" in meta:
                c.https_url(meta["homepage"], "homepage")
            if "icon" in meta:
                c.https_url(meta["icon"], "icon", ".png")
        if not versions:
            c.err("no versions")

        seen.versions, seen.tags = {}, {}
        for path, v in versions:
            rel_path = path.relative_to(repo.root)
            c = _Checker(str(rel_path), errors)
            if c.keys(v, {k: k not in OPTIONAL for k in VERSION_FIELDS}):
                _validate_version(c, mod_id, rel_path, v, seen)
    return errors


@dataclass
class _Seen:
    """What validation has seen so far, to find duplicates and contradictions."""
    weft_by_tag: dict[str, dict]  # weft.toml
    abi_by_commit: dict[str, tuple[str, str]]  # Weft commit -> (ABI, where it was first given)
    revoked: set[str]
    sha: dict[str, str] = field(default_factory=dict)  # sha256 -> version file
    versions: dict[str, Path] = field(default_factory=dict)  # of the current mod
    tags: dict[str, Path] = field(default_factory=dict)  # of the current mod


def _table(c: _Checker, v: dict, name: str) -> bool:
    """Whether the table `name` of a version file has exactly its keys from VERSION_FIELDS."""
    return c.keys(v[name], dict.fromkeys(VERSION_FIELDS[name], True), name)


def _validate_version(c: _Checker, mod_id: str, rel_path: Path, v: dict, seen: _Seen):
    version, tag = v["version"], v["tag"]
    if c.string(version, "version", SEMVER_RE):
        if rel_path.stem != version:
            c.err(f"file name must be {version}.toml")
        if version in seen.versions:
            c.err(f"duplicate version {version} (also in {seen.versions[version]})")
        seen.versions[version] = rel_path
    if c.string(tag, "tag"):
        if tag_version(mod_id, tag) != version:
            c.err(f"tag {tag!r} must be {mod_id}-v{version}")
        if tag in seen.tags:
            c.err(f"duplicate tag {tag}")
        seen.tags[tag] = rel_path
    if c.string(v["channel"], "channel") and isinstance(version, str):
        if v["channel"] != channel_for(version):
            c.err(f"channel must be {channel_for(version)} for version {version}")
    c.time(v["published"], "published")

    source = v["source"]
    src_repo = None
    if _table(c, v, "source"):
        if c.string(source["repo"], "source.repo", REPO_RE):
            src_repo = source["repo"]
        c.string(source["commit"], "source.commit", SHA1_RE)

    weft = v["weft"]
    if _table(c, v, "weft"):
        ok = c.string(weft["tag"], "weft.tag", WEFT_TAG_RE)
        ok &= c.string(weft["commit"], "weft.commit", SHA1_RE)
        ok &= c.string(weft["abi"], "weft.abi", ABI_RE)
        # A revoked build may name a Weft release that weft.toml no longer lists (MAINTAINING.md,
        # "When the weekly publish fails").
        sha = v["asset"].get("sha256") if isinstance(v["asset"], dict) else None
        revoked = isinstance(sha, str) and sha in seen.revoked
        if ok and not revoked:
            _check_weft(c, weft, seen.weft_by_tag, seen.abi_by_commit, str(rel_path))

    deps = v["deps"]
    if not isinstance(deps, list):
        c.err("deps must be a list")
    else:
        dep_ids = []
        for d in deps:
            if c.keys(d, {"id": True}, "deps[]") and c.string(d["id"], "deps[].id", ID_RE):
                dep_ids.append(d["id"])
        if len(set(dep_ids)) != len(dep_ids):
            c.err("duplicate deps")
        if mod_id in dep_ids:
            c.err("a mod can't depend on itself")

    platforms = v["platforms"]
    if not isinstance(platforms, list) or not platforms:
        c.err("platforms must be a non-empty list")
    elif any(p not in PLATFORMS for p in platforms):
        c.err(f"platforms must be among {PLATFORMS}")
    elif platforms != [p for p in PLATFORMS if p in platforms]:
        c.err(f"platforms must be unique and in the order {list(PLATFORMS)}")

    asset = v["asset"]
    if _table(c, v, "asset"):
        if asset["name"] != f"{mod_id}.zip":
            c.err(f"asset.name must be {mod_id}.zip")
        if c.string(asset["url"], "asset.url") and src_repo and isinstance(tag, str):
            prefix = f"https://github.com/{src_repo}/releases/download/"
            expected = expected_asset_url(src_repo, tag, mod_id)
            if not asset["url"].startswith(prefix):
                c.err(f"asset.url must start with {prefix}")
            elif asset["url"] != expected:
                c.err(f"asset.url must be {expected}")
        size = asset["size"]
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            c.err("asset.size must be a positive integer")
        if c.string(asset["sha256"], "asset.sha256", SHA256_RE):
            sha = asset["sha256"]
            if sha in seen.sha:
                c.err(f"asset.sha256 is also pinned by {seen.sha[sha]}")
            seen.sha[sha] = str(rel_path)

    if "provenance" in v:
        p = v["provenance"]
        if _table(c, v, "provenance"):
            if p["kind"] != "github-attestation":
                c.err("provenance.kind must be github-attestation")
            if p["repo"] != src_repo:
                c.err("provenance.repo must equal source.repo")
            c.string(p["workflow"], "provenance.workflow", WORKFLOW_RE)
            c.string(p["ref"], "provenance.ref", GIT_REF_RE)
            c.time(p["verified_at"], "provenance.verified_at")

    r = v["reviewed"]
    if _table(c, v, "reviewed"):
        c.string(r["by"], "reviewed.by", LOGIN_RE)
        tomorrow = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).strftime(DATE_FORMAT)
        if c.time(r["at"], "reviewed.at", DATE_FORMAT) and r["at"] > tomorrow:
            c.err(f"reviewed.at {r['at']} is in the future")


def _check_weft(c, weft: dict, weft_by_tag: dict, abi_by_commit: dict, where: str):
    tag, commit, abi = weft["tag"], weft["commit"], weft["abi"]
    known = weft_by_tag.get(tag)
    if known is not None:
        if known["commit"] != commit:
            c.err(f"weft.commit differs from weft.toml's {tag} ({known['commit']})")
    else:
        m = DESCRIBE_RE.fullmatch(tag)
        if not m:
            c.err(f"weft.tag {tag} is not in weft.toml and not of the form <tag>-<n>-g<sha7>")
        elif m.group(1) not in weft_by_tag:
            c.err(f"weft.tag {tag} is based on {m.group(1)}, which weft.toml doesn't list")
        elif m.group(3) != commit[:7]:
            c.err(f"weft.tag {tag} must end in -g{commit[:7]}")
    prev = abi_by_commit.get(commit)
    if prev is not None and prev[0] != abi:
        c.err(f"weft.abi {abi} differs from {prev[0]} for the same commit in {prev[1]}")
    abi_by_commit.setdefault(commit, (abi, where))


# ---------------------------------------------------------------------------------------------
# Rendering


def _version_json(v: dict) -> dict:
    """A validated version file as index.json has it: every field and table key in
    VERSION_FIELDS order, `null` for an absent table."""
    return {k: None if v.get(k) is None
            else {key: v[k][key] for key in keys} if keys else copy.deepcopy(v[k])
            for k, keys in VERSION_FIELDS.items()}


def version_order_key(v: dict) -> tuple:
    """Sorted descending, newest first: by publication time, then SemVer precedence and tag."""
    return (v["published"], semver_key(v["version"]), v["tag"])


def render(repo: Repo, generated: str) -> dict:
    mods = []
    for mod_id in sorted(repo.mods):
        mod = repo.mods[mod_id]
        vs = sorted((v for _, v in mod.versions), key=version_order_key, reverse=True)
        mods.append({
            "id": mod_id,
            **{k: copy.deepcopy(mod.meta.get(k)) for k in MOD_FIELDS},
            "versions": [_version_json(v) for v in vs],
        })
    weft = sorted(repo.weft, key=lambda r: weft_tag_key(r["tag"]), reverse=True)
    revoked = sorted(repo.revoked, key=lambda r: (r["at"], r["sha256"]))
    return {
        "schema": SCHEMA,
        "generated": generated,
        "weft": [{"tag": r["tag"], "commit": r["commit"], "abi": r["abi"]} for r in weft],
        "mods": mods,
        "revoked": [{"sha256": r["sha256"], "reason": r["reason"], "at": r["at"]}
                    for r in revoked],
    }


def dumps(index: dict) -> bytes:
    """The exact bytes of `index.json`: two-space indent, UTF-8, LF, trailing newline."""
    return (json.dumps(index, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def load_valid(root: Path) -> Repo:
    repo, errors = load(root)
    errors += validate(repo)
    if errors:
        raise ValidationError(errors)
    return repo


def build(root: Path, generated: str) -> bytes:
    errors: list[str] = []
    _Checker("--generated", errors).time(generated, "generated")
    if errors:
        raise ValidationError(errors)
    return dumps(render(load_valid(root), generated))
