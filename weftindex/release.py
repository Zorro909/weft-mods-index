"""What a GitHub release of a mod proves, and `add-release`, which writes it as a version file.

`inspect` gathers every field of a version file from sources the reviewer can see, and checks them
against each other: the GitHub release and its asset digest, the downloaded bytes, the release's
`<id>.mod.json`, the Weft source at the commit it names, and the build provenance attestation if
there is one. `add-release` writes what it returns; `check` runs it again later and compares.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import model, tomlw
from .net import GitHub, NotFound, sha256
from .platforms import zip_platforms
from .weft import Weft, ahead_of, weft_file


class ReleaseError(model.Error):
    pass


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime(model.TIME_FORMAT)


def today_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime(model.DATE_FORMAT)


def provenance(gh: GitHub, repo: str, digest: str, data: bytes, source_commit: str,
               pinned: dict | None = None) -> dict | None:
    """The provenance entry of the bytes, or None if GitHub has no attestation for them. Fails if
    there is one, but none verifies with `gh attestation verify` and was built by a workflow of
    `repo` from the tagged commit or its parent (a release workflow may commit a version bump
    before it tags). Of several that do, the one by the `pinned` workflow and ref, if any."""
    try:
        listed = gh.api(f"/repos/{repo}/attestations/sha256:{digest}")
    except NotFound:
        return None
    if not listed.get("attestations"):
        return None
    signer_re = re.compile(rf"https://github\.com/{re.escape(repo)}/(\.github/workflows/[^@]+)"
                           r"@(refs/.+)")
    found = []
    results = gh.verify_attestation(data, repo)
    for result in results:
        cert = result.get("verificationResult", {}).get("signature", {}).get("certificate", {})
        m = signer_re.fullmatch(cert.get("buildSignerURI", ""))
        built_from = cert.get("sourceRepositoryDigest", "")
        if m and model.SHA1_RE.fullmatch(built_from) \
                and ahead_of(gh, repo, built_from, source_commit) in (0, 1):
            found.append({"kind": "github-attestation", "repo": repo, "workflow": m.group(1),
                          "ref": m.group(2), "verified_at": now_utc()})
    if not found:
        signers = sorted({r.get("verificationResult", {}).get("signature", {}).get("certificate", {})
                          .get("buildSignerURI", "?") for r in results})
        raise ReleaseError(f"no attestation was made by a workflow of {repo} building "
                           f"{source_commit[:7]} or its parent; signers: {', '.join(signers)}")
    same = [p for p in found if pinned and (p["workflow"], p["ref"]) ==
            (pinned.get("workflow"), pinned.get("ref"))]
    return (same or found)[0]


# ---------------------------------------------------------------------------------------------
# inspect


@dataclass
class Release:
    mod_id: str
    entry: dict  # every field of the version file but `reviewed`
    meta: dict  # the mod.toml fields from <id>.mod.json


def _field(data, path: str, name: str):
    for key in path.split("."):
        if not isinstance(data, dict) or key not in data:
            raise ReleaseError(f"{name} has no {path}")
        data = data[key]
    return data


def _expect(data: dict, name: str, expect: dict) -> None:
    """Fails unless each dotted path of `data` has the value `expect` gives it."""
    for path, want in expect.items():
        if (got := _field(data, path, name)) != want:
            raise ReleaseError(f"{name}: {path} is {got!r}, not {want!r}")


def _mod_json(gh: GitHub, rel: dict, mod_id: str) -> dict | None:
    name = f"{mod_id}.mod.json"
    asset = next((a for a in rel["assets"] if a["name"] == name), None)
    if asset is None:
        return None
    try:
        data = json.loads(gh.download(asset["browser_download_url"]))
    except ValueError:
        raise ReleaseError(f"{name} is not JSON") from None
    if not isinstance(data, dict):
        raise ReleaseError(f"{name} is not a JSON object")
    deps = _field(data, "deps", name)
    if not isinstance(deps, list) or not all(isinstance(d, dict) and isinstance(d.get("id"), str)
                                             for d in deps):
        raise ReleaseError(f"{name}: deps must be a list of {{id}}")
    return data


def inspect(gh: GitHub, weft: Weft, repo: str, tag: str, pinned: dict | None = None) -> Release:
    """`pinned` is the version file `check` compares with: its provenance is preferred if several
    attestations verify."""
    parts = model.split_tag(tag)
    if parts is None or not model.SEMVER_RE.fullmatch(parts[1]):
        raise ReleaseError(f"tag {tag!r} is not of the form <id>-v<SemVer>")
    mod_id, version = parts

    rel = gh.api(f"/repos/{repo}/releases/tags/{tag}")
    if rel.get("draft"):
        raise ReleaseError(f"{tag} is a draft")
    asset = next((a for a in rel["assets"] if a["name"] == f"{mod_id}.zip"), None)
    if asset is None:
        raise ReleaseError(f"release {tag} has no {mod_id}.zip")
    url = model.expected_asset_url(repo, tag, mod_id)
    if asset["browser_download_url"] != url:
        raise ReleaseError(f"asset URL {asset['browser_download_url']} is not {url}")
    data = gh.download(url)
    digest = sha256(data)
    if asset.get("digest") != f"sha256:{digest}":
        raise ReleaseError(f"downloaded sha256 {digest}, GitHub's digest is {asset.get('digest')}")
    if len(data) != asset["size"]:
        raise ReleaseError(f"downloaded {len(data)} bytes, GitHub says {asset['size']}")

    source_commit = gh.api(f"/repos/{repo}/commits/{tag}")["sha"]
    target = rel.get("target_commitish", "")
    if model.SHA1_RE.fullmatch(target) and target != source_commit:
        raise ReleaseError(f"tag {tag} is at {source_commit}, the release says {target}")
    channel = model.channel_for(version)
    platforms = zip_platforms(data, mod_id)

    name = f"{mod_id}.mod.json"
    mod_json = _mod_json(gh, rel, mod_id)
    if mod_json is None:
        raise ReleaseError(f"release {tag} has no {name}; see README.md, \"<id>.mod.json\"")
    weft_commit = _field(mod_json, "weft.commit", name)
    weft_name = _field(mod_json, "weft.tag", name)
    if not isinstance(weft_commit, str) or not model.SHA1_RE.fullmatch(weft_commit):
        raise ReleaseError(f"{name}: Weft commit {weft_commit!r} is not a full sha")
    abi = weft.abi(weft_commit)
    if not (isinstance(weft_name, str) and weft.names(weft_name, weft_commit)):
        raise ReleaseError(f"{name}: weft.tag {weft_name!r} doesn't name Weft {weft_commit[:7]}")
    _expect(mod_json, name, {
        "schema": 1, "id": mod_id, "tag": tag, "version": version, "channel": channel,
        "source.repo": repo, "source.commit": source_commit,
        "asset.name": f"{mod_id}.zip", "asset.url": url, "asset.size": len(data),
        "asset.sha256": digest, "weft.abi": abi, "platforms": platforms})

    entry = {
        "version": version,
        "channel": channel,
        "tag": tag,
        "published": rel["published_at"],
        "deps": [{"id": d["id"]} for d in mod_json["deps"]],
        "platforms": platforms,
        "source": {"repo": repo, "commit": source_commit},
        "weft": {"tag": weft_name, "commit": weft_commit, "abi": abi},
        "asset": {"name": f"{mod_id}.zip", "url": url, "size": len(data), "sha256": digest},
        "provenance": provenance(gh, repo, digest, data, source_commit,
                                 pinned.get("provenance") if pinned else None),
    }
    return Release(mod_id, entry, {k: mod_json.get(k) for k in model.MOD_FIELDS})


# ---------------------------------------------------------------------------------------------
# add-release


def _validate_with(root: Path, out: dict[Path, str]) -> None:
    """Validates the repository as it would be with the files `out`, in a scratch copy."""
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        for name in ("weft.toml", "revoked.toml"):
            shutil.copy(root / name, scratch / name)
        if (root / "mods").is_dir():
            shutil.copytree(root / "mods", scratch / "mods")
        for path, text in out.items():
            target = scratch / path.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        model.load_valid(scratch)


def pr_description(current: model.Repo, releases: list[Release]) -> tuple[str, str]:
    """A title and Markdown body for the pull request that adds `releases`."""
    names: dict[str, list[str]] = {}
    lines = []
    for r in releases:
        v, src = r.entry, r.entry["source"]
        names.setdefault(r.mod_id, []).append(v["version"])
        line = (f"- **{r.mod_id} {v['version']}**: "
                f"[release](https://github.com/{src['repo']}/releases/tag/{v['tag']}), "
                f"[source](https://github.com/{src['repo']}/tree/{src['commit']})")
        old = current.mods.get(r.mod_id)
        earlier = [pv for _, pv in old.versions
                   if model.semver_key(pv["version"]) < model.semver_key(v["version"])] if old else []
        if earlier:
            prev = max(earlier, key=lambda pv: model.semver_key(pv["version"]))
            line += (f", [changes since {prev['version']}](https://github.com/{src['repo']}"
                     f"/compare/{prev['source']['commit']}...{src['commit']})")
        elif not old:
            line += ", new mod"
        lines.append(line + (", provenance verified" if v["provenance"] else ", no attestation"))
    title = "Add " + ", ".join(f"{m} {' '.join(vs)}" for m, vs in names.items())
    body = "\n".join([
        *lines, "",
        "Written by `python3 -m weftindex add-release`.", "",
        "Reviewer: read the source (MAINTAINING.md, \"Review\"), then set `reviewed.by` to your "
        "login and `reviewed.at` to today in each version file before you approve.", ""])
    return title, body


def add_release(gh: GitHub, root: Path, repo: str, tags: list[str], reviewer: str | None = None,
                reviewed_at: str | None = None, update_meta: bool = False, force: bool = False,
                log=print) -> tuple[list[Path], str, str]:
    """Writes a version file for each tag, mod.toml for a new mod (from its newest release here),
    and any Weft release they need in weft.toml. Writes nothing unless every tag succeeds and the
    result validates. Returns the files written, and a title and body for the pull request.

    `reviewer` defaults to the caller's GitHub login; whoever approves the PR puts in their own."""
    current, errors = model.load(root)
    if errors:
        raise model.ValidationError(errors)
    for tag in tags:
        parts = model.split_tag(tag)
        path = root / "mods" / parts[0] / "versions" / f"{parts[1]}.toml" if parts else None
        if path is not None and path.exists() and not force:
            raise ReleaseError(f"{path.relative_to(root)} exists (use --force to overwrite)")
    if reviewer is None:
        reviewer = gh.api("/user")["login"]
    weft = Weft(gh)
    weft_entries = {r["tag"]: r for r in current.weft}
    out: dict[Path, str] = {}
    releases: list[Release] = []
    for tag in tags:
        log(f"{tag}: reading the release")
        r = inspect(gh, weft, repo, tag)
        releases.append(r)
        v = r.entry
        path = root / "mods" / r.mod_id / "versions" / f"{v['version']}.toml"
        out[path] = tomlw.version_file(
            {**v, "reviewed": {"by": reviewer, "at": reviewed_at or today_utc()}})
        log(f"{tag}: Weft {v['weft']['tag']}, ABI {v['weft']['abi']}, provenance "
            f"{'verified' if v['provenance'] else 'none (no attestation)'}")
        base = model.weft_base(v["weft"]["tag"])
        if base not in weft_entries:
            weft_entries[base] = weft.release(base)

    for mod_id in dict.fromkeys(r.mod_id for r in releases):
        mod_toml = root / "mods" / mod_id / "mod.toml"
        if update_meta or not mod_toml.exists():
            newest = max((r for r in releases if r.mod_id == mod_id),
                         key=lambda r: model.version_order_key(r.entry))
            out[mod_toml] = tomlw.mod_file(newest.meta)

    if len(weft_entries) > len(current.weft):
        out[root / "weft.toml"] = weft_file(list(weft_entries.values()))
    _validate_with(root, out)
    for path, text in out.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        log(f"wrote {path.relative_to(root)}")
    return list(out), *pr_description(current, releases)
