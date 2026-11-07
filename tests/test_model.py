"""The offline rules (model.py): each file's keys and values, and the rendered index."""

import unittest

from tests.fixtures import ABI, REPO, WEFT_RELEASE, scratch_repo, write_mod
from weftindex import model

SHA = "a" * 64
SHA2 = "b" * 64


class LoadTest(unittest.TestCase):
    def errors(self, root):
        return model.load(root)[1]

    def assertError(self, root, text):
        errors = self.errors(root)
        self.assertTrue(any(text in e for e in errors), errors)

    def test_empty_repository_is_valid(self):
        self.assertEqual(self.errors(scratch_repo(self, [WEFT_RELEASE])), [])

    def test_a_valid_mod(self):
        root = scratch_repo(self)
        write_mod(root, "cogwork", {"0.2.0": SHA, "0.3.0-rc.1": SHA2})
        repo, errors = model.load(root)
        self.assertEqual(errors, [])
        self.assertEqual(repo.mods["cogwork"].versions["0.2.0"],
                         {"repo": REPO, "tag": "cogwork-v0.2.0", "sha256": SHA})

    def test_version_file_rules(self):
        cases = {
            'repo = "x"\ntag = "cogwork-v0.2.0"\nsha256 = "' + SHA + '"\n': "repo 'x'",
            f'repo = "{REPO}"\ntag = "cogwork-v0.2.1"\nsha256 = "{SHA}"\n':
                "tag must be cogwork-v0.2.0",
            f'repo = "{REPO}"\ntag = "cogwork-v0.2.0"\nsha256 = "{SHA.upper()}"\n': "sha256",
            f'repo = "{REPO}"\ntag = "cogwork-v0.2.0"\n': "missing key sha256",
            f'repo = "{REPO}"\ntag = "cogwork-v0.2.0"\nsha256 = "{SHA}"\nurl = "x"\n':
                "unknown key url",
        }
        for text, error in cases.items():
            with self.subTest(error):
                root = scratch_repo(self)
                write_mod(root, "cogwork", {})
                (root / "mods/cogwork/versions/0.2.0.toml").write_text(text)
                self.assertError(root, error)

    def test_version_must_be_semver(self):
        for bad in ("1.0", "01.0.0", "1.0.0+build", "1.0.0-rc.01"):
            with self.subTest(bad):
                root = scratch_repo(self)
                write_mod(root, "cogwork", {bad: SHA})
                self.assertError(root, "the version (file name)")

    def test_mod_rules(self):
        root = scratch_repo(self)
        write_mod(root, "Cog-Work", {"1.0.0": SHA})
        self.assertError(root, "the mod id (folder name)")
        root = scratch_repo(self)
        write_mod(root, "cogwork", {"1.0.0": SHA}, summary="x" * 141)
        self.assertError(root, "more than 140")
        root = scratch_repo(self)
        write_mod(root, "cogwork", {"1.0.0": SHA}, summary="two\nlines")
        self.assertError(root, "one line")
        root = scratch_repo(self)
        write_mod(root, "cogwork", {})
        self.assertError(root, "no versions")
        (root / "mods/cogwork/notes.txt").write_text("")
        self.assertError(root, "unexpected files ['notes.txt']")

    def test_one_repo_per_mod(self):
        root = scratch_repo(self)
        write_mod(root, "cogwork", {"1.0.0": SHA})
        write_mod(root, "cogwork", {"1.1.0": SHA2}, repo="someone/else")
        self.assertError(root, "one mod, one repo")

    def test_sha256_pinned_once(self):
        root = scratch_repo(self)
        write_mod(root, "cogwork", {"1.0.0": SHA, "1.1.0": SHA})
        self.assertError(root, "also pinned by")

    def test_weft_and_revoked_rules(self):
        root = scratch_repo(self, [WEFT_RELEASE, WEFT_RELEASE])
        self.assertError(root, "duplicate tag v26.11.1")
        (root / "revoked.toml").write_text('[[revoked]]\nsha256 = "x"\nat = "2026-11-05"\n')
        self.assertError(root, "revoked.toml revoked #1: unknown key at")
        (root / "revoked.toml").write_text("revoked = 1\n")
        self.assertError(root, "revoked must be an array of tables")


class ManifestTest(unittest.TestCase):
    def manifest(self, **changes):
        return {"id": "cogwork", "name": "Cogwork", "version": "0.2.0", "abi": ABI,
                "deps": ["base"], **changes}

    def test_valid(self):
        self.assertEqual(model.check_manifest("cogwork", "0.2.0", self.manifest(), "m"), [])

    def test_rules(self):
        cases = {
            "id is 'other'": {"id": "other"},
            "version is '0.2.1'": {"version": "0.2.1"},
            "abi": {"abi": "F470B1CBB1F9AD91"},
            "deps must be a list of mod ids": {"deps": ["Base"]},
            "must not include cogwork": {"deps": ["cogwork"]},
            "name must be one line": {"name": "a\nb"},
            "unknown key summary": {"summary": "x"},
        }
        for error, changes in cases.items():
            with self.subTest(error):
                errors = model.check_manifest("cogwork", "0.2.0", self.manifest(**changes), "m")
                self.assertTrue(any(error in e for e in errors), errors)


class RenderTest(unittest.TestCase):
    def test_render(self):
        root = scratch_repo(self, [WEFT_RELEASE, {**WEFT_RELEASE, "tag": "v26.11.10"}])
        write_mod(root, "cogwork", {"0.2.0": SHA, "0.10.0": SHA2, "0.9.0": "c" * 64})
        (root / "revoked.toml").write_text(f'[[revoked]]\nsha256 = "{"c" * 64}"\n')
        repo = model.load_valid(root)
        manifests = {("cogwork", v): {"name": f"Cogwork {v}", "abi": ABI, "deps": ["base"]}
                     for v in ("0.2.0", "0.10.0")}
        index = model.render(repo, manifests)
        self.assertEqual(index["schema"], 2)
        self.assertEqual(list(index["weft"]), ["v26.11.10", "v26.11.1"])
        self.assertEqual(index["revoked"], [{"sha256": "c" * 64}])
        [mod] = index["mods"]
        self.assertEqual(mod["name"], "Cogwork 0.10.0")  # the newest build's
        self.assertEqual([b["version"] for b in mod["builds"]], ["0.10.0", "0.2.0"])
        self.assertEqual(mod["builds"][0], {"version": "0.10.0", "tag": "cogwork-v0.10.0",
                                            "abi": ABI, "deps": ["base"], "sha256": SHA2})
        self.assertEqual(list(mod), ["id", "name", "summary", "repo", "builds"])

    def test_mod_toml_name_wins(self):
        root = scratch_repo(self)
        write_mod(root, "cogwork", {"0.2.0": SHA})
        (root / "mods/cogwork/mod.toml").write_text('summary = "Gears."\nname = "Cog Work"\n')
        index = model.render(model.load_valid(root),
                             {("cogwork", "0.2.0"): {"name": "Cogwork", "abi": ABI, "deps": []}})
        self.assertEqual(index["mods"][0]["name"], "Cog Work")

    def test_dumps(self):
        self.assertEqual(model.dumps({"a": "é"}), '{\n  "a": "é"\n}\n'.encode())


if __name__ == "__main__":
    unittest.main()
