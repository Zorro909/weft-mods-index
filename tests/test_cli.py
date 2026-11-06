"""The CLI (`__main__.py`) and `check --since`'s choice of files."""

import io
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from tests.fixtures import scratch_repo
from weftindex import __main__ as cli
from weftindex import check, model


class ChangedFilesTest(unittest.TestCase):
    def git(self, *args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=self.root,
                       check=True, capture_output=True)

    def test_changed_version_files(self):
        self.root = scratch_repo(self)
        for name in ("a/versions/1.0.0.toml", "b/versions/1.0.0.toml", "b/versions/1.1.0.toml"):
            (self.root / "mods" / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / "mods" / name).write_text("")
        (self.root / "mods/b/mod.toml").write_text("")
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-qm", "base")
        self.git("switch", "-qc", "pr")
        (self.root / "mods/a/versions/1.1.0.toml").write_text("new")
        (self.root / "mods/b/mod.toml").write_text("changed")
        (self.root / "README.md").write_text("docs")
        self.git("add", "-A")
        self.git("commit", "-qm", "pr")
        got = check.changed_version_files(self.root, "main")
        # Not b's versions: a changed mod.toml needs only the offline validation.
        self.assertEqual(sorted(str(p.relative_to(self.root)) for p in got),
                         ["mods/a/versions/1.1.0.toml"])
        with self.assertRaisesRegex(model.Error, "git diff against nope failed"):
            check.changed_version_files(self.root, "nope")


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

    def test_build_writes_under_root(self):
        root = scratch_repo(self)
        code, stdout, _ = self.run_cli("--root", str(root), "build",
                                       "--generated", "2026-11-04T18:00:00Z")
        self.assertEqual(code, 0)
        self.assertIn("0 mods, 0 versions", stdout)
        self.assertTrue((root / "dist/v1/index.json").is_file())


if __name__ == "__main__":
    unittest.main()
