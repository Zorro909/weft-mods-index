"""Loads the reviewed entries from the repository, validates them against the index contract
(see README.md, "Index format"), and renders `v1/index.json`.

Layout of the source of truth:

    weft.toml                          Weft releases and their ABI hash  -> index `weft[]`
    revoked.toml                       revoked sha256 values             -> index `revoked[]`
    mods/<id>/mod.toml                 the mod's name, summary, ...      -> index `mods[]`
    mods/<id>/versions/<version>.toml  one reviewed release              -> `mods[].versions[]`

Everything here is offline: no network access. `check.py` does the online re-verification.
"""

from __future__ import annotations

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
RFC3339_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
# SemVer 2.0 (https://semver.org), without build metadata: `+` can't be in a tag URL unescaped.
_PRE_ID = r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    rf"(?:-({_PRE_ID}(?:\.{_PRE_ID})*))?$"
)
# An SPDX license expression made of ids, `AND`, `OR`, `WITH` and parentheses.
SPDX_RE = re.compile(r"^[A-Za-z0-9.\-+()]+(?:\s+(?:AND|OR|WITH)\s+[A-Za-z0-9.\-+()]+)*$")
WORKFLOW_RE = re.compile(r"^\.github/workflows/[A-Za-z0-9._-]+\.ya?ml$")
GIT_REF_RE = re.compile(r"^refs/(?:heads|tags)/[A-Za-z0-9._/\-]+$")

CHANNELS = ("release", "prerelease", "experimental")
# The order platforms are listed in.
PLATFORMS = ("windows-x64", "linux-x64", "macos-arm64")
SUMMARY_MAX = 140
# Experimental builds carry `-experimental.<weft7>.<mod7>` in their tag, and since a version
# file's version is its tag without `<id>-v`, in their version too.
EXPERIMENTAL_RE = re.compile(r"-experimental\.[0-9a-f]{7}\.[0-9a-f]{7}$")
# `git describe --tags` of a Weft commit that is not itself a release: `<tag>-<n>-g<sha7>`.
DESCRIBE_RE = re.compile(r"^(v[0-9A-Za-z.\-]+?)-(\d+)-g([0-9a-f]{7})$")

MOD_KEYS = {"name": True, "summary": True, "authors": True, "license": False,
            "homepage": False, "icon": False}
VERSION_KEYS = {"version": True, "channel": True, "tag": True, "published": True,
                "source": True, "weft": True, "deps": True, "platforms": True, "asset": True,
                "provenance": False, "reviewed": True}


class ValidationError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


@dataclass
class Repo:
    """The parsed source of truth, before validation."""
    root: Path
    weft: list[dict] = field(default_factory=list)
    revoked: list[dict] = field(default_factory=list)
    # id -> (mod.toml data, [(path, version data)])
    mods: dict[str, tuple[dict, list[tuple[Path, dict]]]] = field(default_factory=dict)

    def versions(self):
        """Every (mod id, path, version data)."""
        for mod_id, (_, versions) in self.mods.items():
            for path, v in versions:
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


def channel_for(version: str, github_prerelease: bool) -> str:
    """The channel a release goes into: experimental builds by their tag, then anything GitHub
    calls a prerelease or that has a SemVer prerelease part."""
    if EXPERIMENTAL_RE.search(version):
        return "experimental"
    if github_prerelease or "-" in version:
        return "prerelease"
    return "release"


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
    except (OSError, tomllib.TOMLDecodeError) as e:
        errors.append(f"{path}: {e}")
        return None


def load(root: Path) -> tuple[Repo, list[str]]:
    errors: list[str] = []
    repo = Repo(root=root)
    for name, key in (("weft.toml", "release"), ("revoked.toml", "revoked")):
        data = _load_toml(root / name, errors) if (root / name).exists() else {}
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
    mods_dir = root / "mods"
    mod_dirs = sorted(p for p in mods_dir.iterdir() if p.is_dir()) if mods_dir.is_dir() else []
    for mod_dir in mod_dirs:
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
        repo.mods[mod_dir.name] = (meta, versions)
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
        if pattern is not None and not pattern.match(value):
            self.err(f"{what} {value!r} does not match {pattern.pattern}")
            return False
        return True

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
        c.string(rev["at"], "at", DATE_RE)

    seen_sha: dict[str, str] = {}
    abi_by_commit = {commit: (abi, "weft.toml") for commit, abi in weft_commits.items()}
    for mod_id, (meta, versions) in repo.mods.items():
        c = _Checker(f"mods/{mod_id}/mod.toml", errors)
        if not ID_RE.match(mod_id):
            c.err(f"mod id {mod_id!r} (the folder name) does not match {ID_RE.pattern}")
        if c.keys(meta, MOD_KEYS):
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

        seen_versions: dict[str, Path] = {}
        seen_tags: dict[str, Path] = {}
        for path, v in versions:
            rel_path = path.relative_to(repo.root)
            c = _Checker(str(rel_path), errors)
            if not c.keys(v, VERSION_KEYS):
                continue
            _validate_version(c, mod_id, rel_path, v, seen_versions, seen_tags, seen_sha,
                              weft_by_tag, abi_by_commit)
    return errors


def _validate_version(c, mod_id, rel_path, v, seen_versions, seen_tags, seen_sha, weft_by_tag,
                      abi_by_commit):
    version, tag = v["version"], v["tag"]
    if c.string(version, "version", SEMVER_RE):
        if rel_path.stem != version:
            c.err(f"file name must be {version}.toml")
        if version in seen_versions:
            c.err(f"duplicate version {version} (also in {seen_versions[version]})")
        seen_versions[version] = rel_path
    if c.string(tag, "tag"):
        if tag_version(mod_id, tag) != version:
            c.err(f"tag {tag!r} must be {mod_id}-v{version}")
        if tag in seen_tags:
            c.err(f"duplicate tag {tag}")
        seen_tags[tag] = rel_path
    if c.string(v["channel"], "channel") and isinstance(version, str):
        experimental = bool(EXPERIMENTAL_RE.search(version))
        if v["channel"] not in CHANNELS:
            c.err(f"channel must be one of {CHANNELS}")
        elif experimental and v["channel"] != "experimental":
            c.err("an experimental build's channel must be experimental")
        elif not experimental and v["channel"] == "experimental":
            c.err("channel experimental needs an -experimental.<weft7>.<mod7> version")
        elif "-" in version and v["channel"] == "release":
            c.err("a prerelease version can't be in channel release")
    c.string(v["published"], "published", RFC3339_UTC_RE)

    source = v["source"]
    src_repo = None
    if c.keys(source, {"repo": True, "commit": True}, "source"):
        if c.string(source["repo"], "source.repo", REPO_RE):
            src_repo = source["repo"]
        c.string(source["commit"], "source.commit", SHA1_RE)

    weft = v["weft"]
    if c.keys(weft, {"tag": True, "commit": True, "abi": True}, "weft"):
        ok = c.string(weft["tag"], "weft.tag", WEFT_TAG_RE)
        ok &= c.string(weft["commit"], "weft.commit", SHA1_RE)
        ok &= c.string(weft["abi"], "weft.abi", ABI_RE)
        if ok:
            _check_weft(c, weft, weft_by_tag, abi_by_commit, str(rel_path))

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
    if c.keys(asset, {"name": True, "url": True, "size": True, "sha256": True}, "asset"):
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
            if sha in seen_sha:
                c.err(f"asset.sha256 is also pinned by {seen_sha[sha]}")
            seen_sha[sha] = str(rel_path)

    if "provenance" in v:
        p = v["provenance"]
        spec = {"kind": True, "repo": True, "workflow": True, "ref": True, "verified_at": True}
        if c.keys(p, spec, "provenance"):
            if p["kind"] != "github-attestation":
                c.err("provenance.kind must be github-attestation")
            if p["repo"] != src_repo:
                c.err("provenance.repo must equal source.repo")
            c.string(p["workflow"], "provenance.workflow", WORKFLOW_RE)
            c.string(p["ref"], "provenance.ref", GIT_REF_RE)
            c.string(p["verified_at"], "provenance.verified_at", RFC3339_UTC_RE)

    r = v["reviewed"]
    if c.keys(r, {"by": True, "at": True}, "reviewed"):
        c.string(r["by"], "reviewed.by", LOGIN_RE)
        c.string(r["at"], "reviewed.at", DATE_RE)


def _check_weft(c, weft: dict, weft_by_tag: dict, abi_by_commit: dict, where: str):
    tag, commit, abi = weft["tag"], weft["commit"], weft["abi"]
    known = weft_by_tag.get(tag)
    if known is not None:
        if known["commit"] != commit:
            c.err(f"weft.commit differs from weft.toml's {tag} ({known['commit']})")
    else:
        m = DESCRIBE_RE.match(tag)
        if not m:
            c.err(f"weft.tag {tag} is not in weft.toml and not of the form <tag>-<n>-g<sha7>")
        elif m.group(1) not in weft_by_tag:
            c.err(f"weft.tag {tag} is based on {m.group(1)}, which weft.toml doesn't list")
        elif m.group(3) != commit[:7]:
            c.err(f"weft.tag {tag} must end in -g{commit[:7]}")
        elif commit in {r["commit"] for r in weft_by_tag.values()}:
            c.err(f"weft.commit {commit[:7]} is a release; weft.tag must be its tag")
    prev = abi_by_commit.get(commit)
    if prev is not None and prev[0] != abi:
        c.err(f"weft.abi {abi} differs from {prev[0]} for the same commit in {prev[1]}")
    abi_by_commit.setdefault(commit, (abi, where))


# ---------------------------------------------------------------------------------------------
# Rendering


def _version_json(v: dict) -> dict:
    p = v.get("provenance")
    return {
        "version": v["version"],
        "channel": v["channel"],
        "tag": v["tag"],
        "published": v["published"],
        "source": {"repo": v["source"]["repo"], "commit": v["source"]["commit"]},
        "weft": {"tag": v["weft"]["tag"], "commit": v["weft"]["commit"],
                 "abi": v["weft"]["abi"]},
        "deps": [{"id": d["id"]} for d in v["deps"]],
        "platforms": list(v["platforms"]),
        "asset": {"name": v["asset"]["name"], "url": v["asset"]["url"],
                  "size": v["asset"]["size"], "sha256": v["asset"]["sha256"]},
        "provenance": None if p is None else {
            "kind": p["kind"], "repo": p["repo"], "workflow": p["workflow"], "ref": p["ref"],
            "verified_at": p["verified_at"],
        },
        "reviewed": {"by": v["reviewed"]["by"], "at": v["reviewed"]["at"]},
    }


def version_order_key(v: dict) -> tuple:
    """Sorted descending, newest first: by publication time, then SemVer precedence and tag."""
    return (v["published"], semver_key(v["version"]), v["tag"])


def render(repo: Repo, generated: str) -> dict:
    mods = []
    for mod_id in sorted(repo.mods):
        meta, versions = repo.mods[mod_id]
        vs = sorted((v for _, v in versions), key=version_order_key, reverse=True)
        mods.append({
            "id": mod_id,
            "name": meta["name"],
            "summary": meta["summary"],
            "authors": list(meta["authors"]),
            "license": meta.get("license"),
            "homepage": meta.get("homepage"),
            "icon": meta.get("icon"),
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
    if not RFC3339_UTC_RE.match(generated):
        raise ValidationError([f"generated {generated!r} must be RFC 3339 UTC (…Z)"])
    return dumps(render(load_valid(root), generated))
