"""The CLI (`__main__.py`)."""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from tests.fixtures import WEFT_RELEASE, FakeGitHub, scratch_repo, write_mod
from weftindex import __main__ as cli
from weftindex import model


class CliTest(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_invalid_repository_is_one_line_per_error(self):
        root = scratch_repo(self)
        (root / "revoked.toml").write_text("revoked = 1\n")
        code, _, err = self.run_cli("--root", str(root), "validate")
        self.assertEqual(code, 1)
        self.assertIn("error: revoked.toml: revoked must be an array of tables", err)
        self.assertNotIn("Traceback", err)

    def test_network_error_is_one_line(self):
        with mock.patch.object(cli, "_github", side_effect=model.Error("GET /user: HTTP 401")):
            code, _, err = self.run_cli("--root", str(scratch_repo(self)), "sync-weft")
        self.assertEqual((code, err), (1, "error: GET /user: HTTP 401\n"))

    def test_build_writes_v2(self):
        gh = FakeGitHub()
        root = scratch_repo(self, [WEFT_RELEASE])
        write_mod(root, "cogwork", {"0.2.0": gh.publish()})
        with mock.patch.object(cli, "_github", return_value=gh):
            code, stdout, err = self.run_cli("--root", str(root), "build")
        self.assertEqual(code, 0, err)
        self.assertIn("1 mods, 1 builds, 1 Weft releases, 0 revoked", stdout)
        index = json.loads((root / "dist/v2/index.json").read_text())
        self.assertEqual(index["mods"][0]["id"], "cogwork")

    def test_add_release_writes_the_pr_description(self):
        gh = FakeGitHub()
        gh.publish()
        root = scratch_repo(self, [WEFT_RELEASE])
        with mock.patch.object(cli, "_github", return_value=gh):
            code, stdout, err = self.run_cli("--root", str(root), "add-release",
                                             "Zorro909/weft-mods", "cogwork-v0.2.0",
                                             "--summary", "Gears.")
        self.assertEqual(code, 0, err)
        self.assertEqual((root / "dist/pr-title.txt").read_text(), "Add cogwork 0.2.0\n")
        self.assertIn("gh pr create", stdout)


if __name__ == "__main__":
    unittest.main()
