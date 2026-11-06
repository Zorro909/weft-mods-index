"""`add-release` and `check` end to end, against a fake GitHub."""

import copy
import hashlib
import json
import tomllib
import unittest
from pathlib import Path

from tests.fixtures import WEFT, elf, macho, pe, scratch_repo, zip_of
from weftindex import check, model, release, tomlw
from weftindex.abi import ABI_FILE, abi_hash
from weftindex.net import NotFound
from weftindex.platforms import zip_platforms
from weftindex.weft import WEFT_REPO

REPO = "Zorro909/weft-mods"
TAG = "cogwork-v0.2.0"
COMMIT = "c" * 40
PARENT = "b" * 40
DEV = "4563878b067425085701268f074f9483b2625e4c"  # a Weft commit 46 after v26.11.1
WEFT_ABI = abi_hash(b"pub struct ModDecl;\n")
WEFT_TAGS = f"/repos/{WEFT_REPO}/tags"
ZIP = zip_of({"cogwork.dll": pe(), "libcogwork.so": elf(), "libcogwork.dylib": macho()})


def url(tag):
    return f"https://github.com/{REPO}/releases/download/{tag}/cogwork.zip"


class FakeGitHub:
    """The five methods of `net.GitHub`, answered from dicts."""

    def __init__(self):
        self.api_responses = {}
        self.downloads = {}
        self.files = {}
        self.attestations = []
        self.calls = []

    def api(self, path):
        self.calls.append(path)
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

    def verify_attestation(self, data, repo):
        return copy.deepcopy(self.attestations)

    # Setting up the world.

    def publish(self, version="0.2.0", zip_bytes=ZIP, commit=COMMIT, mod_json=..., **mj):
        """A release of cogwork `version` with an `<id>.mod.json` that agrees with it, changed by
        `mj` (a field set to None is left out); or with none for `mod_json=None`. Returns its tag."""
        tag = f"cogwork-v{version}"
        sha = hashlib.sha256(zip_bytes).hexdigest()
        assets = [{"name": "cogwork.zip", "browser_download_url": url(tag), "size": len(zip_bytes),
                   "digest": f"sha256:{sha}"}]
        self.downloads[url(tag)] = zip_bytes
        if mod_json is ...:
            mod_json = {
                "schema": 1, "id": "cogwork", "name": "Cogwork", "summary": "Gears.",
                "authors": ["Zorro909"], "license": "MIT", "homepage": "https://example.com/cog",
                "icon": None, "version": version, "channel": model.channel_for(version),
                "tag": tag, "published": "2026-11-04T02:00:00Z",
                "source": {"repo": REPO, "commit": commit},
                "weft": {"tag": "v26.11.1", "commit": WEFT, "abi": WEFT_ABI},
                "deps": [{"id": "base"}], "platforms": zip_platforms(zip_bytes, "cogwork"),
                "asset": {"name": "cogwork.zip", "url": url(tag), "size": len(zip_bytes),
                          "sha256": sha},
                **mj}
            mod_json = {k: v for k, v in mod_json.items() if not (k in mj and v is None)}
        if mod_json is not None:
            mj_url = url(tag).replace("cogwork.zip", "cogwork.mod.json")
            assets.append({"name": "cogwork.mod.json", "browser_download_url": mj_url})
            self.downloads[mj_url] = json.dumps(mod_json).encode()
        self.api_responses[f"/repos/{REPO}/releases/tags/{tag}"] = {
            "tag_name": tag, "draft": False, "prerelease": False, "target_commitish": "main",
            "published_at": "2026-11-04T02:39:03Z", "assets": assets}
        self.api_responses[f"/repos/{REPO}/commits/{tag}"] = {"sha": commit}
        return tag

    def attest(self, built_from, ahead_by, workflow="release.yml"):
        sha = hashlib.sha256(self.downloads[url(TAG)]).hexdigest()
        self.api_responses[f"/repos/{REPO}/attestations/sha256:{sha}"] = {"attestations": [{}]}
        self.api_responses[f"/repos/{REPO}/compare/{built_from}...{COMMIT}"] = {
            "status": "ahead" if ahead_by else "identical", "ahead_by": ahead_by}
        self.attestations.append({"verificationResult": {"signature": {"certificate": {
            "buildSignerURI": f"https://github.com/{REPO}/.github/workflows/{workflow}"
                              "@refs/heads/main",
            "sourceRepositoryDigest": built_from}}}})


def world() -> FakeGitHub:
    gh = FakeGitHub()
    gh.api_responses["/user"] = {"login": "a-modder"}
    gh.api_responses[WEFT_TAGS] = [{"name": "v26.11.1", "commit": {"sha": WEFT}},
                                   {"name": "not-a-release", "commit": {"sha": "0" * 40}}]
    gh.api_responses[f"/repos/{WEFT_REPO}/compare/v26.11.1...{DEV}"] = {
        "status": "ahead", "ahead_by": 46}
    gh.files[(WEFT_REPO, WEFT, ABI_FILE)] = b"pub struct ModDecl;\n"
    gh.files[(WEFT_REPO, DEV, ABI_FILE)] = b"pub struct ModDecl;\n"
    gh.publish()
    return gh


class OnlineTest(unittest.TestCase):
    def setUp(self):
        self.root = scratch_repo(self)
        self.gh = world()

    def add(self, *tags, **kw):
        kw.setdefault("reviewed_at", "2026-11-05")
        return release.add_release(self.gh, self.root, REPO, list(tags or [TAG]),
                                   log=lambda *_: None, **kw)

    def version_file(self):
        return tomllib.loads((self.root / "mods/cogwork/versions/0.2.0.toml").read_text())

    def check(self, only=None):
        return check.check(self.gh, self.root, only, log=lambda *_: None)

    def files(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file())

    def assertAnyError(self, fragment, errors):
        self.assertTrue(any(fragment in e for e in errors), f"no {fragment!r} in {errors}")


class AddReleaseTest(OnlineTest):
    def test_writes_version_mod_and_weft(self):
        written, title, body = self.add()
        self.assertEqual(len(written), 3)
        model.load_valid(self.root)
        v = self.version_file()
        self.assertEqual(v["source"], {"repo": REPO, "commit": COMMIT})
        self.assertEqual(v["weft"], {"tag": "v26.11.1", "commit": WEFT, "abi": WEFT_ABI})
        self.assertEqual(v["deps"], [{"id": "base"}])
        self.assertEqual(v["asset"]["sha256"], hashlib.sha256(ZIP).hexdigest())
        self.assertNotIn("provenance", v)
        meta = tomllib.loads((self.root / "mods/cogwork/mod.toml").read_text())
        self.assertEqual((meta["summary"], meta["license"]), ("Gears.", "MIT"))
        # Only the Weft release this version needs, not every tag.
        weft = tomllib.loads((self.root / "weft.toml").read_text())["release"]
        self.assertEqual([r["tag"] for r in weft], ["v26.11.1"])
        self.assertEqual(title, "Add cogwork 0.2.0")
        self.assertIn(f"https://github.com/{REPO}/releases/tag/{TAG}", body)
        self.assertIn("new mod", body)

    def test_mod_json_is_required(self):
        self.gh.publish(mod_json=None)
        with self.assertRaisesRegex(release.ReleaseError, "has no cogwork.mod.json"):
            self.add()

    def test_describe_name(self):
        self.gh.publish(weft={"tag": "v26.11.1-46-g4563878", "commit": DEV, "abi": WEFT_ABI})
        self.add()
        self.assertEqual(self.version_file()["weft"]["tag"], "v26.11.1-46-g4563878")

    def test_a_release_tag_needs_no_compare(self):
        self.add()
        self.assertFalse([c for c in self.gh.calls if "/compare/" in c], self.gh.calls)

    def test_github_prerelease_flag_doesnt_set_the_channel(self):
        self.gh.api_responses[f"/repos/{REPO}/releases/tags/{TAG}"]["prerelease"] = True
        self.add()
        self.assertEqual(self.version_file()["channel"], "release")

    def test_reviewer_defaults_to_the_caller(self):
        self.add()
        self.assertEqual(self.version_file()["reviewed"], {"by": "a-modder", "at": "2026-11-05"})

    def test_pr_links_changes_since_the_last_version(self):
        self.add()
        tag2 = self.gh.publish("0.2.1", zip_of({"libcogwork.so": elf()}), commit="d" * 40)
        _, title, body = self.add(tag2)
        self.assertEqual(title, "Add cogwork 0.2.1")
        self.assertIn(f"[changes since 0.2.0](https://github.com/{REPO}/compare/{COMMIT}..."
                      f"{'d' * 40})", body)

    def test_batch_takes_metadata_from_the_newest_release(self):
        tag2 = self.gh.publish("0.2.1", zip_of({"libcogwork.so": elf()}), summary="Newer.")
        self.add(tag2, TAG)
        meta = tomllib.loads((self.root / "mods/cogwork/mod.toml").read_text())
        self.assertEqual(meta["summary"], "Newer.")

    def test_same_zip_twice_is_refused(self):
        self.add()
        tag2 = self.gh.publish("0.2.1")
        with self.assertRaisesRegex(model.ValidationError, "is also pinned by"):
            self.add(tag2)

    def test_mod_json_must_agree(self):
        sha = hashlib.sha256(ZIP).hexdigest()
        asset = {"name": "cogwork.zip", "url": url(TAG), "size": len(ZIP), "sha256": sha}
        for over, field in (
                (dict(asset={**asset, "sha256": "0" * 64}), "asset.sha256"),
                (dict(asset={**asset, "url": "https://x/y.zip"}), "asset.url"),
                (dict(source={"repo": "other/repo", "commit": COMMIT}), "source.repo"),
                (dict(weft={"tag": "v26.10.1", "commit": WEFT, "abi": WEFT_ABI}), "weft.tag"),
                (dict(weft={"tag": "v26.11.1", "commit": WEFT, "abi": "0" * 16}), "weft.abi"),
                (dict(channel="prerelease"), "channel"),
                (dict(platforms=["linux-x64"]), "platforms")):
            self.gh.publish(**over)
            with self.subTest(field), self.assertRaisesRegex(release.ReleaseError, field):
                self.add()

    def test_mod_json_missing_field(self):
        self.gh.publish(weft=None)
        with self.assertRaisesRegex(release.ReleaseError, "has no weft.commit"):
            self.add()

    def test_mod_json_bad_type_names_the_field(self):
        self.gh.publish(license=3.14)
        with self.assertRaisesRegex(model.Error, "license: can't write a float"):
            self.add()

    def test_writes_nothing_when_a_tag_fails(self):
        with self.assertRaises(model.Error):
            self.add(TAG, "cogwork-v0.3.0")
        self.assertEqual(self.files(), ["revoked.toml", "weft.toml"])

    def test_writes_nothing_when_the_result_is_invalid(self):
        self.gh.publish(summary="x" * 141)
        with self.assertRaisesRegex(model.ValidationError, "more than 140"):
            self.add()
        self.assertEqual(self.files(), ["revoked.toml", "weft.toml"])

    def test_existing_version_is_refused_before_any_download(self):
        self.add()
        self.gh.calls.clear()
        with self.assertRaisesRegex(release.ReleaseError, "exists"):
            self.add()
        self.assertEqual(self.gh.calls, [])
        self.add(force=True)

    def test_digest_must_match_bytes(self):
        self.gh.downloads[url(TAG)] = ZIP + b"x"
        with self.assertRaisesRegex(release.ReleaseError, "GitHub's digest"):
            self.add()

    def test_provenance_from_tagged_commit_or_parent(self):
        self.gh.attest(PARENT, 1)
        self.add()
        p = self.version_file()["provenance"]
        self.assertEqual((p["workflow"], p["ref"]),
                         (".github/workflows/release.yml", "refs/heads/main"))

    def test_provenance_from_an_older_commit_is_refused(self):
        self.gh.attest(PARENT, 2)
        with self.assertRaisesRegex(release.ReleaseError, "or its parent; signers: https://"):
            self.add()


class CheckTest(OnlineTest):
    def setUp(self):
        super().setUp()
        self.add()

    def test_unchanged_release_passes(self):
        self.assertEqual(self.check(), [])

    def test_replaced_asset(self):
        self.gh.publish(zip_bytes=ZIP + b"x")
        self.assertAnyError("asset.sha256 is pinned as", self.check())

    def test_moved_tag(self):
        self.gh.publish(commit="d" * 40)
        self.assertAnyError("source.commit is pinned as", self.check())

    def test_attestation_that_was_not_pinned(self):
        self.gh.attest(COMMIT, 0)
        self.assertAnyError("provenance.kind is pinned as None", self.check())

    def test_pinned_attestation_is_preferred(self):
        self.gh.attest(COMMIT, 0, "release.yml")
        self.add(force=True)
        self.gh.attestations.insert(0, copy.deepcopy(self.gh.attestations[0]))
        cert = self.gh.attestations[0]["verificationResult"]["signature"]["certificate"]
        cert["buildSignerURI"] = cert["buildSignerURI"].replace("release.yml", "other.yml")
        self.assertEqual(self.check(), [])

    def test_deleted_release(self):
        del self.gh.api_responses[f"/repos/{REPO}/releases/tags/{TAG}"]
        self.assertAnyError("not found", self.check())

    def test_github_prerelease_flag_can_change(self):
        self.gh.api_responses[f"/repos/{REPO}/releases/tags/{TAG}"]["prerelease"] = True
        self.assertEqual(self.check(), [])

    def test_changed_weft_abi(self):
        self.gh.files[(WEFT_REPO, WEFT, ABI_FILE)] = b"changed"
        self.assertAnyError("weft.abi is", self.check())

    def test_revoked_is_skipped(self):
        sha = hashlib.sha256(ZIP).hexdigest()
        (self.root / "revoked.toml").write_text(
            f'[[revoked]]\nsha256 = "{sha}"\nreason = "bad"\nat = "2026-11-05"\n')
        self.gh.publish(zip_bytes=ZIP + b"x")
        self.assertEqual(self.check(), [])

    def test_moved_weft_tag_and_its_recovery(self):
        self.gh.api_responses[WEFT_TAGS][0]["commit"]["sha"] = "e" * 40
        self.assertAnyError("weft.toml: v26.11.1", self.check())
        # MAINTAINING.md: revoke what uses it, and drop it from weft.toml.
        sha = hashlib.sha256(ZIP).hexdigest()
        (self.root / "revoked.toml").write_text(
            f'[[revoked]]\nsha256 = "{sha}"\nreason = "Weft v26.11.1 moved"\nat = "2026-11-05"\n')
        (self.root / "weft.toml").write_text("")
        self.assertEqual(self.check(), [])

    def test_removed_mod_json(self):
        self.gh.publish(mod_json=None)
        self.assertAnyError("has no cogwork.mod.json", self.check())

    def test_mod_json_bad_unicode(self):
        self.gh.publish(name="\ud800")
        with self.assertRaisesRegex(model.Error, "name: not valid Unicode"):
            self.add(force=True, update_meta=True)


class WeftNameTest(OnlineTest):
    """A pinned `<tag>-<n>-g<sha7>` stays valid when Weft adds a tag later."""

    def setUp(self):
        super().setUp()
        self.gh.publish(weft={"tag": "v26.11.1-46-g4563878", "commit": DEV, "abi": WEFT_ABI})
        self.add()

    def test_passes(self):
        self.assertEqual(self.check(), [])

    def test_new_tag_on_the_pinned_commit(self):
        self.gh.api_responses[WEFT_TAGS].append({"name": "v26.11.2", "commit": {"sha": DEV}})
        self.assertEqual(self.check(), [])

    def test_wrong_distance_fails(self):
        self.gh.api_responses[f"/repos/{WEFT_REPO}/compare/v26.11.1...{DEV}"]["ahead_by"] = 47
        self.assertAnyError("weft.tag 'v26.11.1-46-g4563878' doesn't name Weft 4563878",
                            self.check())


class LayoutTest(unittest.TestCase):
    def test_repository_files_are_in_the_writers_layout(self):
        """A hand edit that reformats a version file would make the next add-release diff noisy."""
        root = Path(__file__).resolve().parent.parent
        for _, path, v in model.load_valid(root).versions():
            self.assertEqual(tomlw.version_file(v), path.read_text(encoding="utf-8"), path)


if __name__ == "__main__":
    unittest.main()
