"""What a mod release proves (`inspect`), the index built from every release (`build`), and
`add-release`, which writes the files of new releases.

A release of `<repo>` at `<tag>` must have:
  - `<id>.zip`: `weft-mod.toml`, `<id>.dll` / `lib<id>.so` / `lib<id>.dylib`, `assets/...`;
  - `<id>.mod.json`: `{"attestation": <Sigstore bundle>}`, a build provenance attestation of
    the zip, signed by a GitHub Actions workflow of `<repo>`.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import model, tomlw
from .net import GitHub, sha256

ISSUER = "https://token.actions.githubusercontent.com"
MANIFEST = "weft-mod.toml"
TAG_PARTS_RE = re.compile(r"([a-z0-9_]{1,64})-v(.+)")


class ReleaseError(model.Error):
    pass


@dataclass
class Build:
    mod_id: str
    version: str
    repo: str
    tag: str
    sha256: str
    manifest: dict  # weft-mod.toml
    commit: str  # the commit the attestation says was built
    workflow: str  # the workflow that built it: the certificate's SAN


def read_zip(data: bytes, mod_id: str) -> dict:
    """The `weft-mod.toml` of a mod zip, after checking that the zip holds only what it may."""
    allowed = {MANIFEST, f"{mod_id}.dll", f"lib{mod_id}.so", f"lib{mod_id}.dylib"}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
            for name in names:
                parts = name.removesuffix("/").split("/")
                if "\\" in name or any(p in ("", ".", "..") for p in parts) \
                        or not (name in allowed or parts[0] == "assets"):
                    raise ReleaseError(f"{mod_id}.zip may not hold {name!r}")
            if len(set(names)) != len(names):
                raise ReleaseError(f"{mod_id}.zip holds a file twice")
            if not (allowed - {MANIFEST}) & set(names):
                raise ReleaseError(f"{mod_id}.zip holds no library named after {mod_id}")
            if MANIFEST not in names:
                raise ReleaseError(f"{mod_id}.zip has no {MANIFEST}")
            text = z.read(MANIFEST).decode("utf-8")
    except (zipfile.BadZipFile, UnicodeDecodeError, OSError) as e:
        raise ReleaseError(f"{mod_id}.zip: {e}") from None
    try:
        return tomllib.loads(text)
    except ValueError as e:
        raise ReleaseError(f"{mod_id}.zip: {MANIFEST}: {e}") from None


def _provenance(results: list[dict], repo: str) -> tuple[str, str]:
    """(commit, workflow) of the first verified attestation signed as the launcher requires."""
    for result in results:
        cert = result.get("verificationResult", {}).get("signature", {}).get("certificate", {})
        san, commit = cert.get("subjectAlternativeName", ""), cert.get("sourceRepositoryDigest", "")
        if cert.get("issuer") == ISSUER and san.startswith(f"https://github.com/{repo}/") \
                and model.SHA1_RE.fullmatch(commit):
            return commit, san
    raise ReleaseError(f"no attestation was signed by a GitHub Actions workflow of {repo}")


def inspect(gh: GitHub, mod_id: str, version: str, repo: str, tag: str,
            pinned: str | None = None) -> Build:
    """Downloads the release, checks it, and returns what it proves. `pinned` is the sha256 the
    version file pins, which the zip must have."""
    where = f"{repo} {tag}"
    data = gh.download(model.zip_url(repo, tag, mod_id))
    digest = sha256(data)
    if pinned is not None and digest != pinned:
        raise ReleaseError(f"{where}: {mod_id}.zip has sha256 {digest}, the index pins {pinned}")
    try:
        mod_json = json.loads(gh.download(model.zip_url(repo, tag, mod_id, ".mod.json")))
    except ValueError:
        raise ReleaseError(f"{where}: {mod_id}.mod.json is not JSON") from None
    if not isinstance(mod_json, dict) or set(mod_json) != {"attestation"} \
            or not isinstance(mod_json["attestation"], dict):
        raise ReleaseError(f'{where}: {mod_id}.mod.json must be {{"attestation": <bundle>}}')
    commit, workflow = _provenance(gh.verify_attestation(data, mod_json["attestation"], repo), repo)
    manifest = read_zip(data, mod_id)
    errors = model.check_manifest(mod_id, version, manifest, f"{where}: {MANIFEST}")
    if errors:
        raise model.ValidationError(errors)
    return Build(mod_id, version, repo, tag, digest, manifest, commit, workflow)


def missing_deps(builds: list[Build], mod_ids: set[str]) -> list[str]:
    known = mod_ids | {model.BASE}
    return [f"{b.repo} {b.tag}: depends on {d}, which the index doesn't list"
            for b in builds for d in b.manifest["deps"] if d not in known]


def report_line(b: Build) -> str:
    return (f"- **{b.mod_id} {b.version}** ([release](https://github.com/{b.repo}/releases/tag/"
            f"{b.tag})): built from [{b.commit[:7]}](https://github.com/{b.repo}/tree/{b.commit})"
            f" by `{b.workflow}`; ABI `{b.manifest['abi']}`, deps "
            f"{', '.join(b.manifest['deps']) or 'none'}")


# ---------------------------------------------------------------------------------------------
# build


def build(gh: GitHub, root: Path, log=print) -> bytes:
    """`v2/index.json`. Fails unless every release that isn't revoked verifies."""
    from .weft import verify as verify_weft

    repo = model.load_valid(root)
    errors = verify_weft(gh, repo.weft)
    revoked = repo.revoked_shas()
    builds: list[Build] = []
    for mod_id, mod in repo.mods.items():
        for version, v in mod.versions.items():
            if v["sha256"] in revoked:
                log(f"- {mod_id} {version}: revoked, left out")
                continue
            try:
                builds.append(b := inspect(gh, mod_id, version, v["repo"], v["tag"], v["sha256"]))
                log(report_line(b))
            except model.ValidationError as e:
                errors += e.errors
            except model.Error as e:
                errors.append(str(e))
    errors += missing_deps(builds, {b.mod_id for b in builds})
    if errors:
        raise model.ValidationError(errors)
    return model.dumps(model.render(repo, {(b.mod_id, b.version): b.manifest for b in builds}))


# ---------------------------------------------------------------------------------------------
# add-release


def _validate_with(root: Path, out: dict[Path, str]) -> model.Repo:
    """The repository as it would be with the files `out`, validated in a scratch copy."""
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
        return model.load_valid(scratch)


def pr_description(current: model.Repo, builds: list[Build]) -> tuple[str, str]:
    """A title and Markdown body for the pull request that adds `builds`."""
    names: dict[str, list[str]] = {}
    lines = []
    for b in builds:
        names.setdefault(b.mod_id, []).append(b.version)
        line = report_line(b)
        old = current.mods.get(b.mod_id)
        earlier = [v for v in old.versions if model.semver_key(v) < model.semver_key(b.version)] \
            if old else []
        if earlier:
            prev = old.versions[max(earlier, key=model.semver_key)]["tag"]
            line += f"; [changes since {prev}](https://github.com/{b.repo}/compare/{prev}...{b.tag})"
        elif not old:
            line += "; new mod"
        lines.append(line)
    title = "Add " + ", ".join(f"{m} {' '.join(vs)}" for m, vs in names.items())
    body = "\n".join([*lines, "", "Written by `python3 -m weftindex add-release`. Reviewer: read "
                      "the source at the commit each build was made from (MAINTAINING.md, "
                      "\"Review\") before you approve.", ""])
    return title, body


def add_release(gh: GitHub, root: Path, repo: str, tags: list[str], summary: str | None = None,
                log=print) -> tuple[list[Path], str, str]:
    """Writes `mods/<id>/versions/<version>.toml` for each tag, and `mods/<id>/mod.toml` for a mod
    new to the index (with `summary`). Writes nothing unless every tag verifies and the result is
    valid. Returns the files written, and a title and body for the pull request."""
    current = model.load_valid(root)
    out: dict[Path, str] = {}
    builds: list[Build] = []
    for tag in tags:
        m = TAG_PARTS_RE.fullmatch(tag)
        if not m:
            raise ReleaseError(f"tag {tag!r} is not of the form <id>-v<version>")
        mod_id, version = m.groups()
        path = root / "mods" / mod_id / "versions" / f"{version}.toml"
        if path.exists():
            raise ReleaseError(f"{path.relative_to(root)} exists")
        log(f"{tag}: downloading and verifying")
        builds.append(b := inspect(gh, mod_id, version, repo, tag))
        out[path] = tomlw.document({"repo": repo, "tag": tag, "sha256": b.sha256})
        mod_toml = root / "mods" / mod_id / "mod.toml"
        if not mod_toml.exists():
            if not summary:
                raise ReleaseError(f"{mod_id} is new to the index: pass --summary, one line "
                                   f"of at most {model.SUMMARY_MAX} characters")
            out[mod_toml] = tomlw.document({"summary": summary})
    after = _validate_with(root, out)
    ids = {mod_id for mod_id, mod in after.mods.items()
           if any(v["sha256"] not in after.revoked_shas() for v in mod.versions.values())}
    errors = missing_deps(builds, ids)
    if errors:
        raise model.ValidationError(errors)
    for path, text in out.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        log(f"wrote {path.relative_to(root)}")
    return list(out), *pr_description(current, builds)
