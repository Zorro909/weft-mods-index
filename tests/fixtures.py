"""Test data shared by the test modules: mod zips, a scratch index repo, and a fake GitHub."""

import copy
import hashlib
import io
import json
import tempfile
import zipfile
from pathlib import Path

from weftindex import model, tomlw
from weftindex.abi import ABI_FILE, abi_hash
from weftindex.net import NotFound
from weftindex.weft import WEFT_REPO

REPO = "Zorro909/weft-mods"
COMMIT = "c" * 40
ABI = "f470b1cbb1f9ad91"
WEFT_COMMIT = "ff9c7128511fc0dce7719b67c9a59274d70bdd5a"
WEFT_SRC = b"pub struct ModDecl;\n"
WEFT_RELEASE = {"tag": "v26.11.1", "commit": WEFT_COMMIT, "abi": abi_hash(WEFT_SRC)}


def zip_of(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def manifest(mod_id="cogwork", version="0.2.0", deps=("base",), **fields) -> bytes:
    data = {"id": mod_id, "name": mod_id.title(), "version": version, "abi": ABI,
            "deps": list(deps), **fields}
    lines = [f"{k} = {json.dumps(v)}" for k, v in data.items() if v is not None]
    return ("\n".join(lines) + "\n").encode()


def mod_zip(mod_id="cogwork", version="0.2.0", deps=("base",), extra=None, **fields) -> bytes:
    return zip_of({"weft-mod.toml": manifest(mod_id, version, deps, **fields),
                   f"{mod_id}.dll": b"MZ", f"lib{mod_id}.so": b"\x7fELF",
                   "assets/lang/en_us.lang": b"", **(extra or {})})


def scratch_repo(test, weft: list[dict] | None = None) -> Path:
    """An empty index repository that is removed after `test`."""
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    root = Path(tmp.name)
    (root / "weft.toml").write_text(tomlw.array_of_tables("release", weft) if weft else "")
    (root / "revoked.toml").write_text("")
    (root / "mods").mkdir()
    return root


def write_mod(root: Path, mod_id: str, versions: dict[str, str], summary="Gears.", repo=REPO):
    """mods/<id>/ with a version file per (version -> sha256)."""
    (root / "mods" / mod_id / "versions").mkdir(parents=True, exist_ok=True)
    (root / "mods" / mod_id / "mod.toml").write_text(tomlw.document({"summary": summary}))
    for version, sha in versions.items():
        (root / "mods" / mod_id / "versions" / f"{version}.toml").write_text(
            tomlw.document({"repo": repo, "tag": f"{mod_id}-v{version}", "sha256": sha}))


def cert(repo=REPO, commit=COMMIT, issuer="https://token.actions.githubusercontent.com"):
    return {"verificationResult": {"signature": {"certificate": {
        "issuer": issuer, "sourceRepositoryDigest": commit,
        "subjectAlternativeName":
            f"https://github.com/{repo}/.github/workflows/release.yml@refs/heads/main"}}}}


class FakeGitHub:
    """The five methods of `net.GitHub`, answered from dicts. An attestation verifies if its
    bundle is `{"for": <sha256 of the bytes>}`."""

    def __init__(self):
        self.api_responses = {f"/repos/{WEFT_REPO}/tags": [
            {"name": WEFT_RELEASE["tag"], "commit": {"sha": WEFT_COMMIT}}]}
        self.downloads = {}
        self.files = {(WEFT_REPO, WEFT_COMMIT, ABI_FILE): WEFT_SRC}
        self.results = [cert()]

    def api(self, path):
        if path not in self.api_responses:
            raise NotFound(f"GET {path}: not found")
        return copy.deepcopy(self.api_responses[path])

    def api_pages(self, path):
        return self.api(path)

    def download(self, url):
        if url not in self.downloads:
            raise NotFound(f"GET {url}: not found")
        return self.downloads[url]

    def raw(self, repo, commit, path):
        return self.files.get((repo, commit, path))

    def verify_attestation(self, data, bundle, repo):
        if bundle != {"for": hashlib.sha256(data).hexdigest()}:
            raise model.Error("gh attestation verify failed")
        return copy.deepcopy(self.results)

    def publish(self, zip_bytes=None, mod_id="cogwork", version="0.2.0", repo=REPO,
                mod_json=...) -> str:
        """A release `<id>-v<version>` of `zip_bytes` with its attestation. Returns its sha256."""
        zip_bytes = zip_bytes if zip_bytes is not None else mod_zip(mod_id, version)
        tag = f"{mod_id}-v{version}"
        sha = hashlib.sha256(zip_bytes).hexdigest()
        self.downloads[model.zip_url(repo, tag, mod_id)] = zip_bytes
        if mod_json is ...:
            mod_json = {"attestation": {"for": sha}}
        if mod_json is not None:
            self.downloads[model.zip_url(repo, tag, mod_id, ".mod.json")] = \
                json.dumps(mod_json).encode()
        return sha
