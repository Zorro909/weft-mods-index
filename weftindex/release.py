"""`add-release`: turns a GitHub release of a mod into a reviewed version file.

Everything that goes into the file is taken from, or checked against, a source the reviewer can
see: the GitHub release and its asset digest, the downloaded bytes, the mod's `<id>.mod.json`
(or else the "Built against Weft" line of the release notes), the Weft source at that commit, and
the build provenance attestation if there is one.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import struct
import subprocess
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path

from . import model, tomlw
from .abi import ABI_FILE, abi_hash
from .net import GitHub, NotFound, sha256

WEFT_REPO = "Weft-Rust/Weft"
NOTES_RE = re.compile(
    r"Built against Weft (?P<version>[0-9A-Za-z.\-]+), commit "
    r"\[(?P<short>[0-9a-f]{7,40})\]\(https://github\.com/(?P<repo>[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)"
    r"/commit/(?P<commit>[0-9a-f]{40})\)"
)
DECLARE_RE = re.compile(
    r"(?:#\[cfg\((?P<cfg>[^\]]*)\)\]\s*)?declare_mod!\s*\(\s*deps\s*:\s*\[(?P<deps>[^\]]*)\]"
)


class ReleaseError(Exception):
    pass


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------------------------
# Weft


def weft_abi(gh: GitHub, commit: str) -> str:
    src = gh.raw(WEFT_REPO, commit, ABI_FILE)
    if src is None:
        raise ReleaseError(f"{WEFT_REPO} has no {ABI_FILE} at {commit}: unresolvable Weft commit")
    return abi_hash(src)


def weft_tags(gh: GitHub) -> dict[str, str]:
    """Every release tag of Weft that the index accepts, and its commit."""
    return {t["name"]: t["commit"]["sha"] for t in gh.api_pages(f"/repos/{WEFT_REPO}/tags")
            if model.WEFT_TAG_RE.match(t["name"])}


def describe(commit: str, tags: dict[str, str], ahead_of) -> str:
    """The `git describe --tags` name of a Weft commit: its tag if it is a release, else
    `<nearest tag>-<commits since>-g<sha7>`. `ahead_of(tag, commit)` is how many commits `commit`
    has that `tag` doesn't, or None if `tag` is not an ancestor."""
    exact = sorted((t for t, c in tags.items() if c == commit), key=model.weft_tag_key)
    if exact:
        return exact[-1]
    best = None
    for tag in sorted(tags, key=model.weft_tag_key, reverse=True):
        n = ahead_of(tag, commit)
        if n is not None and (best is None or n < best[1]):
            best = (tag, n)
    if best is None:
        raise ReleaseError(f"Weft commit {commit} doesn't descend from any Weft release tag")
    return f"{best[0]}-{best[1]}-g{commit[:7]}"


def github_ahead_of(gh: GitHub, repo: str):
    def ahead_of(base: str, head: str) -> int | None:
        r = gh.api(f"/repos/{repo}/compare/{base}...{head}")
        return r["ahead_by"] if r["status"] in ("ahead", "identical") else None
    return ahead_of


def sync_weft(gh: GitHub, root: Path, tags: dict[str, str] | None = None) -> list[str]:
    """Adds every Weft release missing from weft.toml. Returns the tags added."""
    repo, errors = model.load(root)
    if errors:
        raise ReleaseError("\n".join(errors))
    tags = weft_tags(gh) if tags is None else tags
    have = {r["tag"] for r in repo.weft}
    added = [t for t in tags if t not in have]
    entries = list(repo.weft) + [{"tag": t, "commit": tags[t], "abi": weft_abi(gh, tags[t])}
                                 for t in added]
    write_weft(root, entries)
    return added


WEFT_HEADER = """\
Weft releases and the mod ABI hash of each: FNV-1a-64 of crates/abi/src/lib.rs at the commit.
Becomes the index's `weft[]`. `python3 -m weftindex sync-weft` adds new releases."""


def write_weft(root: Path, entries: list[dict]) -> None:
    entries = sorted(entries, key=lambda r: model.weft_tag_key(r["tag"]), reverse=True)
    entries = [{"tag": r["tag"], "commit": r["commit"], "abi": r["abi"]} for r in entries]
    (root / "weft.toml").write_text(tomlw.array_of_tables("release", entries, WEFT_HEADER))


# ---------------------------------------------------------------------------------------------
# The zip


def _pe_platform(head: bytes) -> str | None:
    if head[:2] != b"MZ" or len(head) < 0x40:
        return None
    off = struct.unpack_from("<I", head, 0x3C)[0]
    if head[off:off + 4] != b"PE\0\0":
        return None
    machine = struct.unpack_from("<H", head, off + 4)[0]
    return "windows-x64" if machine == 0x8664 else None


def _elf_platform(head: bytes) -> str | None:
    if head[:4] != b"\x7fELF" or head[4] != 2 or head[5] != 1:
        return None
    machine = struct.unpack_from("<H", head, 0x12)[0]
    return "linux-x64" if machine == 62 else None


def _macho_platform(head: bytes) -> str | None:
    arm64 = 0x0100000C
    if head[:4] == b"\xcf\xfa\xed\xfe":
        return "macos-arm64" if struct.unpack_from("<I", head, 4)[0] == arm64 else None
    if head[:4] == b"\xca\xfe\xba\xbe":  # universal binary
        n = struct.unpack_from(">I", head, 4)[0]
        for i in range(min(n, 16)):
            if 8 + 20 * i + 4 <= len(head) and struct.unpack_from(">I", head, 8 + 20 * i)[0] == arm64:
                return "macos-arm64"
    return None


def zip_platforms(data: bytes, mod_id: str) -> list[str]:
    """The platforms whose library is at the top of the zip, by its file name and its header."""
    found = set()
    checks = {f"{mod_id}.dll": _pe_platform, f"lib{mod_id}.so": _elf_platform,
              f"lib{mod_id}.dylib": _macho_platform}
    try:
        with zipfile.ZipFile(BytesIO(data)) as z:
            names = set(z.namelist())
            for name, check in checks.items():
                if name in names:
                    with z.open(name) as f:
                        platform = check(f.read(4096))
                    if platform is None:
                        raise ReleaseError(f"{name} in the zip is not a library for a known platform")
                    found.add(platform)
    except zipfile.BadZipFile as e:
        raise ReleaseError(f"{mod_id}.zip is not a zip: {e}") from None
    if not found:
        raise ReleaseError(f"{mod_id}.zip holds no library named after {mod_id}")
    return [p for p in model.PLATFORMS if p in found]


# ---------------------------------------------------------------------------------------------
# Metadata from the mod's source


def parse_lang(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def summarize(description: str, limit: int = model.SUMMARY_MAX) -> str:
    """One line of at most `limit` characters: the description's first sentence, without color
    markup, shortened at a comma or word if it is longer."""
    text = re.sub(r"\{[^{}]*\}", "", description)
    text = re.split(r"\\n|\n", text)[0].strip()
    text = re.split(r"(?<=[.!?])\s+", text)[0].strip()
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    comma = cut.rfind(", ")
    space = cut.rfind(" ")
    end = comma if comma >= limit // 2 else space if space > 0 else limit - 1
    return cut[:end].rstrip(" ,;:") + "…"


def parse_deps(lib_rs: str) -> list[str]:
    """The deps of the mod as a release builds it: the `declare_mod!` without a feature gate, or
    the one for builds without a feature."""
    for m in DECLARE_RE.finditer(lib_rs):
        cfg = (m.group("cfg") or "").replace(" ", "")
        if not cfg or cfg.startswith("not(feature"):
            return re.findall(r'"([a-z0-9_]+)"', m.group("deps"))
    raise ReleaseError("no declare_mod!(deps: [...]) without a feature gate in src/lib.rs")


def mod_folder(gh: GitHub, repo: str, commit: str, mod_id: str) -> str:
    """The mod's folder in its repository: `<id>/` in a repo of several mods, or the root."""
    for folder in (mod_id, ""):
        cargo = gh.raw(repo, commit, f"{folder}/Cargo.toml" if folder else "Cargo.toml")
        if cargo is not None and re.search(rf'^name\s*=\s*"{mod_id}"', cargo.decode(), re.M):
            return folder
    raise ReleaseError(f"can't find the crate {mod_id} in {repo} at {commit}")


def meta_from_source(gh: GitHub, repo: str, commit: str, mod_id: str, folder: str,
                     default_branch: str) -> dict:
    prefix = f"{folder}/" if folder else ""
    lang_raw = gh.raw(repo, commit, f"{prefix}assets/lang/en_us.lang")
    lang = parse_lang(lang_raw.decode("utf-8")) if lang_raw else {}
    name = lang.get(f"mod.{mod_id}.name") or mod_id
    description = lang.get(f"mod.{mod_id}.description", "")
    authors = [a.strip() for a in lang.get(f"mod.{mod_id}.authors", "").split(",") if a.strip()]
    if not authors:
        authors = [repo.split("/")[0]]
    icon_path = f"{prefix}assets/textures/gui/icon.png"
    icon = gh.raw(repo, commit, icon_path)
    return {
        "name": name,
        "summary": summarize(description) if description else name,
        "authors": authors,
        "license": None,
        "homepage": f"https://github.com/{repo}/tree/{default_branch}/{folder}".rstrip("/"),
        "icon": f"https://raw.githubusercontent.com/{repo}/{commit}/{icon_path}"
        if icon is not None and icon[:8] == b"\x89PNG\r\n\x1a\n" else None,
    }


# ---------------------------------------------------------------------------------------------
# Provenance


def gh_has_attestation() -> bool:
    if not shutil.which("gh"):
        return False
    return subprocess.run(["gh", "attestation", "verify", "--help"],
                          capture_output=True).returncode == 0


def verify_attestation(gh: GitHub, repo: str, digest: str, data: bytes, source_commit: str,
                       signer_workflow: str | None = None) -> dict | None:
    """Runs `gh attestation verify` on the bytes if GitHub has an attestation for them. Returns the
    provenance entry, or None if there is no attestation. Fails if there is one that doesn't
    verify, or that wasn't made by a workflow run of an ancestor of the tagged source commit."""
    try:
        listed = gh.api(f"/repos/{repo}/attestations/sha256:{digest}")
    except NotFound:
        return None
    if not listed.get("attestations"):
        return None
    if not gh_has_attestation():
        raise ReleaseError(f"{repo} has an attestation for {digest[:12]}…, but this `gh` has no "
                           "`gh attestation verify` (needs gh >= 2.49)")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "asset.zip"
        path.write_bytes(data)
        cmd = ["gh", "attestation", "verify", str(path), "--repo", repo, "--format", "json"]
        if signer_workflow:
            cmd += ["--signer-workflow", f"{repo}/{signer_workflow}"]
        r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise ReleaseError(f"gh attestation verify failed:\n{r.stderr.strip()}")
    results = json.loads(r.stdout)
    ahead_of = github_ahead_of(gh, repo)
    for result in results:
        cert = result["verificationResult"]["signature"]["certificate"]
        signer = cert.get("buildSignerURI", "")
        m = re.match(rf"^https://github\.com/{re.escape(repo)}/(\.github/workflows/[^@]+)@(refs/.+)$",
                     signer)
        built_from = cert.get("sourceRepositoryDigest", "")
        if not m or not model.SHA1_RE.match(built_from):
            continue
        # A release workflow may commit a version bump before it builds: the tag then points at
        # a child of the commit the workflow ran for.
        if ahead_of(built_from, source_commit) is None:
            continue
        return {"kind": "github-attestation", "repo": repo, "workflow": m.group(1),
                "ref": m.group(2), "verified_at": now_utc()}
    raise ReleaseError("no attestation was signed by a workflow of the repo for the tagged source")


# ---------------------------------------------------------------------------------------------
# add-release


def _mod_json(gh: GitHub, rel: dict, mod_id: str) -> dict | None:
    asset = next((a for a in rel["assets"] if a["name"] == f"{mod_id}.mod.json"), None)
    if asset is None:
        return None
    data = json.loads(gh.download(asset["browser_download_url"]))
    if data.get("schema") != 1 or data.get("id") != mod_id or data.get("tag") != rel["tag_name"]:
        raise ReleaseError(f"{mod_id}.mod.json: schema must be 1, id {mod_id}, tag {rel['tag_name']}")
    return data


def add_release(gh: GitHub, root: Path, repo: str, tag: str, reviewer: str,
                reviewed_at: str | None = None, update_meta: bool = False,
                force: bool = False, log=print) -> Path:
    parts = model.split_tag(tag)
    if parts is None:
        raise ReleaseError(f"tag {tag!r} is not of the form <id>-v<version>")
    mod_id, version = parts
    if not model.ID_RE.match(mod_id) or not model.SEMVER_RE.match(version):
        raise ReleaseError(f"tag {tag!r}: bad mod id or version")
    path = root / "mods" / mod_id / "versions" / f"{version}.toml"
    if path.exists() and not force:
        raise ReleaseError(f"{path.relative_to(root)} exists (use --force to overwrite)")

    rel = gh.api(f"/repos/{repo}/releases/tags/{tag}")
    if rel.get("draft"):
        raise ReleaseError(f"{tag} is a draft")
    asset = next((a for a in rel["assets"] if a["name"] == f"{mod_id}.zip"), None)
    if asset is None:
        raise ReleaseError(f"release {tag} has no {mod_id}.zip")
    url = model.expected_asset_url(repo, tag, mod_id)
    if asset["browser_download_url"] != url:
        raise ReleaseError(f"asset URL {asset['browser_download_url']} is not {url}")
    digest = asset.get("digest") or ""
    if not digest.startswith("sha256:"):
        raise ReleaseError(f"GitHub has no sha256 digest for {mod_id}.zip")
    digest = digest.removeprefix("sha256:")

    log(f"downloading {url}")
    data = gh.download(url)
    actual = sha256(data)
    if actual != digest:
        raise ReleaseError(f"downloaded sha256 {actual} != GitHub digest {digest}")
    if len(data) != asset["size"]:
        raise ReleaseError(f"downloaded {len(data)} bytes, GitHub says {asset['size']}")

    source_commit = gh.api(f"/repos/{repo}/commits/{tag}")["sha"]
    target = rel.get("target_commitish", "")
    if model.SHA1_RE.match(target) and target != source_commit:
        raise ReleaseError(f"tag {tag} is at {source_commit}, the release says {target}")

    mod_json = _mod_json(gh, rel, mod_id)
    if mod_json is not None:
        log(f"using {mod_id}.mod.json")
        weft_commit = mod_json["weft"]["commit"]
        if mod_json.get("version") != version:
            raise ReleaseError(f"{mod_id}.mod.json version {mod_json.get('version')} != {version}")
        if mod_json.get("source", {}).get("commit") != source_commit:
            raise ReleaseError(f"{mod_id}.mod.json source.commit != tag commit {source_commit}")
        mj_asset = mod_json.get("asset", {})
        if mj_asset.get("sha256") != actual or mj_asset.get("size") != len(data):
            raise ReleaseError(f"{mod_id}.mod.json asset sha256/size don't match the zip")
    else:
        m = NOTES_RE.search(rel.get("body") or "")
        if m is None:
            raise ReleaseError(f"no {mod_id}.mod.json and no 'Built against Weft' line in the notes")
        if m.group("repo") != WEFT_REPO or not m.group("commit").startswith(m.group("short")):
            raise ReleaseError(f"the notes link to {m.group('repo')}, not {WEFT_REPO}")
        weft_commit = m.group("commit")
    if not model.SHA1_RE.match(weft_commit or ""):
        raise ReleaseError(f"Weft commit {weft_commit!r} is not a full sha")

    abi = weft_abi(gh, weft_commit)
    if mod_json is not None and mod_json["weft"].get("abi") != abi:
        raise ReleaseError(f"{mod_id}.mod.json says ABI {mod_json['weft'].get('abi')}, "
                           f"Weft at {weft_commit[:7]} hashes to {abi}")
    tags = weft_tags(gh)
    sync_weft(gh, root, tags)
    weft_tag = describe(weft_commit, tags, github_ahead_of(gh, WEFT_REPO))
    log(f"Weft {weft_tag} ({weft_commit[:7]}), ABI {abi}")

    channel = model.channel_for(version, rel["prerelease"])
    if mod_json is not None and mod_json.get("channel") != channel:
        raise ReleaseError(f"{mod_id}.mod.json channel {mod_json.get('channel')} != {channel}")

    platforms = zip_platforms(data, mod_id)
    if mod_json is not None and sorted(mod_json.get("platforms", [])) != sorted(platforms):
        raise ReleaseError(f"{mod_id}.mod.json platforms {mod_json.get('platforms')} != "
                           f"the zip's {platforms}")

    folder = mod_folder(gh, repo, source_commit, mod_id)
    if mod_json is not None:
        deps = [d["id"] for d in mod_json.get("deps", [])]
    else:
        lib_rs = gh.raw(repo, source_commit, f"{folder}/src/lib.rs" if folder else "src/lib.rs")
        if lib_rs is None:
            raise ReleaseError("no src/lib.rs to read the deps from")
        deps = parse_deps(lib_rs.decode("utf-8"))

    provenance = verify_attestation(gh, repo, actual, data, source_commit)
    log(f"provenance: {'verified' if provenance else 'none (no attestation)'}")

    entry = {
        "version": version,
        "channel": channel,
        "tag": tag,
        "published": rel["published_at"],
        "deps": [{"id": d} for d in deps],
        "platforms": platforms,
        "source": {"repo": repo, "commit": source_commit},
        "weft": {"tag": weft_tag, "commit": weft_commit, "abi": abi},
        "asset": {"name": f"{mod_id}.zip", "url": url, "size": len(data), "sha256": actual},
        "provenance": provenance,
        "reviewed": {"by": reviewer, "at": reviewed_at or today_utc()},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlw.version_file(entry))
    log(f"wrote {path.relative_to(root)}")

    mod_toml = root / "mods" / mod_id / "mod.toml"
    if update_meta or not mod_toml.exists():
        default_branch = gh.api(f"/repos/{repo}")["default_branch"]
        meta = meta_from_source(gh, repo, source_commit, mod_id, folder, default_branch)
        if mod_json is not None:
            for k in ("name", "summary", "authors", "license", "homepage", "icon"):
                if k in mod_json:
                    meta[k] = mod_json[k]
        mod_toml.write_text(tomlw.mod_file(meta))
        log(f"wrote {mod_toml.relative_to(root)}")
    return path
