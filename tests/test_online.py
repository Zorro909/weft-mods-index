"""`inspect`, `build` and `add-release` end to end, against a fake GitHub."""

import json
import unittest

from tests.fixtures import (ABI, COMMIT, REPO, WEFT_RELEASE, FakeGitHub, cert, mod_zip,
                            scratch_repo, write_mod, zip_of)
from weftindex import model, release


def logs():
    out = []
    return out, out.append


class InspectTest(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()

    def inspect(self, pinned=None, **kw):
        return release.inspect(self.gh, "cogwork", "0.2.0", REPO, "cogwork-v0.2.0", pinned, **kw)

    def assertFails(self, text, **kw):
        with self.assertRaises(model.Error) as cm:
            self.inspect(**kw)
        self.assertIn(text, str(cm.exception))

    def test_a_good_release(self):
        sha = self.gh.publish()
        b = self.inspect(sha)
        self.assertEqual((b.sha256, b.commit, b.manifest["abi"], b.manifest["deps"]),
                         (sha, COMMIT, ABI, ["base"]))
        self.assertTrue(b.workflow.startswith(f"https://github.com/{REPO}/.github/workflows/"))

    def test_pinned_sha256(self):
        self.gh.publish()
        self.assertFails("the index pins " + "a" * 64, pinned="a" * 64)

    def test_mod_json(self):
        self.gh.publish(mod_json=None)
        self.assertFails("not found")
        self.gh.publish(mod_json={"attestation": {}, "weft": {}})
        self.assertFails('must be {"attestation": <bundle>}')
        self.gh.publish(mod_json={"attestation": {"for": "someone else's zip"}})
        self.assertFails("gh attestation verify failed")

    def test_other_files_are_allowed(self):
        self.gh.publish(mod_zip(extra={"README.md": b"", "data/x.bin": b"", "other.dll": b""}))
        self.assertEqual(self.inspect().manifest["id"], "cogwork")

    def test_attestation_signer(self):
        self.gh.publish()
        for result in (cert(repo="someone/else"), cert(issuer="https://evil"), cert(commit="x")):
            with self.subTest(result):
                self.gh.results = [result]
                self.assertFails(f"no attestation was signed by a GitHub Actions workflow of {REPO}")

    def test_zip_contents(self):
        cases = {
            "may not hold 'assets/../x'": mod_zip(extra={"assets/../x": b""}),
            "may not hold '/etc/x'": mod_zip(extra={"/etc/x": b""}),
            "may not hold 'a\\\\b'": mod_zip(extra={"a\\b": b""}),
            "no library": zip_of({"weft-mod.toml": b"", "assets/a": b""}),
            "has no weft-mod.toml": zip_of({"cogwork.dll": b""}),
            "weft-mod.toml:": zip_of({"weft-mod.toml": b"id = ", "cogwork.dll": b""}),
            "version is '0.3.0'": mod_zip(version="0.3.0"),
            "is not a zip": b"not a zip",
        }
        for error, data in cases.items():
            with self.subTest(error):
                self.gh.publish(data)
                self.assertFails(error)


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.root = scratch_repo(self, [WEFT_RELEASE])

    def build(self):
        out, log = logs()
        return json.loads(release.build(self.gh, self.root, log)), out

    def test_build(self):
        write_mod(self.root, "cogwork", {"0.2.0": self.gh.publish()})
        write_mod(self.root, "structures", {
            "0.1.0": self.gh.publish(mod_zip("structures", "0.1.0", deps=["base", "cogwork"]),
                                     "structures", "0.1.0")}, summary="Builds.")
        index, out = self.build()
        self.assertEqual(index["weft"], {"v26.11.1": WEFT_RELEASE["abi"]})
        self.assertEqual([m["id"] for m in index["mods"]], ["cogwork", "structures"])
        self.assertEqual(index["mods"][1]["builds"][0]["deps"], ["base", "cogwork"])
        self.assertIn(f"built from [ccccccc](https://github.com/{REPO}/tree/{COMMIT})", out[0])

    def test_deps_must_be_in_the_index(self):
        write_mod(self.root, "cogwork", {"0.2.0": self.gh.publish(mod_zip(deps=["power"]))})
        with self.assertRaisesRegex(model.Error, "depends on power, which the index doesn't"):
            self.build()

    def test_revoked_builds_are_left_out_and_not_downloaded(self):
        sha = "d" * 64
        write_mod(self.root, "cogwork", {"0.2.0": self.gh.publish(), "0.1.0": sha})
        (self.root / "revoked.toml").write_text(f'[[revoked]]\nsha256 = "{sha}"\nreason = "Bad"\n')
        index, _ = self.build()
        self.assertEqual([b["version"] for b in index["mods"][0]["builds"]], ["0.2.0"])
        self.assertEqual(index["revoked"], [{"sha256": sha, "reason": "Bad"}])

    def test_every_error_is_reported(self):
        write_mod(self.root, "cogwork", {"0.2.0": "a" * 64, "0.3.0": "b" * 64})
        self.gh.api_responses["/repos/Weft-Rust/Weft/tags"] = []
        with self.assertRaises(model.ValidationError) as cm:
            self.build()
        self.assertEqual(len(cm.exception.errors), 3, cm.exception.errors)
        self.assertIn("has no tag v26.11.1", cm.exception.errors[0])

    def test_moved_weft_tag(self):
        self.gh.api_responses["/repos/Weft-Rust/Weft/tags"] = [
            {"name": "v26.11.1", "commit": {"sha": "e" * 40}}]
        self.gh.files[("Weft-Rust/Weft", "e" * 40, "crates/abi/src/lib.rs")] = b"changed"
        with self.assertRaisesRegex(model.Error, "v26.11.1 is eeee"):
            self.build()


class AddReleaseTest(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.root = scratch_repo(self, [WEFT_RELEASE])

    def add(self, *tags, summary=None):
        return release.add_release(self.gh, self.root, REPO, list(tags), summary, log=lambda _: 0)

    def test_new_mod(self):
        sha = self.gh.publish()
        with self.assertRaisesRegex(model.Error, "cogwork is new to the index: pass --summary"):
            self.add("cogwork-v0.2.0")
        files, title, body = self.add("cogwork-v0.2.0", summary="Gears.")
        self.assertEqual(title, "Add cogwork 0.2.0")
        self.assertIn("new mod", body)
        self.assertEqual((self.root / "mods/cogwork/mod.toml").read_text(), 'summary = "Gears."\n')
        self.assertEqual((self.root / "mods/cogwork/versions/0.2.0.toml").read_text(),
                         f'repo = "{REPO}"\ntag = "cogwork-v0.2.0"\nsha256 = "{sha}"\n')
        self.assertEqual(len(files), 2)

    def test_next_version(self):
        self.gh.publish()
        self.add("cogwork-v0.2.0", summary="Gears.")
        self.gh.publish(mod_zip(version="0.3.0"), version="0.3.0")
        files, _, body = self.add("cogwork-v0.3.0")
        self.assertEqual(files, [self.root / "mods/cogwork/versions/0.3.0.toml"])
        self.assertIn("compare/cogwork-v0.2.0...cogwork-v0.3.0", body)
        with self.assertRaisesRegex(model.Error, "exists"):
            self.add("cogwork-v0.3.0")

    def test_writes_nothing_on_failure(self):
        self.gh.publish(mod_zip(deps=["power"]))
        with self.assertRaisesRegex(model.Error, "depends on power"):
            self.add("cogwork-v0.2.0", summary="Gears.")
        self.assertFalse((self.root / "mods/cogwork").exists())

    def test_bad_tag(self):
        with self.assertRaisesRegex(model.Error, "not of the form"):
            self.add("v0.2.0")


if __name__ == "__main__":
    unittest.main()
