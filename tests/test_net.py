"""The real GitHub adapter (net.py), with urlopen and subprocess replaced."""

import io
import json
import subprocess
import unittest
import urllib.error
import http.client
from unittest import mock

from weftindex import model, net


def http_error(code):
    e = urllib.error.HTTPError("https://x", code, "nope", {}, io.BytesIO())
    e.close()
    return e


class Response(io.BytesIO):
    headers: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class GitHubTest(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.answers = []
        patcher = mock.patch("urllib.request.urlopen", self.urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleep = mock.patch("time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)
        self.gh = net.GitHub(token="secret")

    def urlopen(self, req, timeout):
        self.requests.append(req)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, Response) else Response(answer)

    def test_token_goes_only_to_the_api(self):
        self.answers = [b'{"a": 1}', b"zip bytes"]
        self.assertEqual(self.gh.api("/repos/x/y"), {"a": 1})
        self.assertEqual(self.gh.download("https://github.com/x/y/releases/download/t/y.zip"),
                         b"zip bytes")
        api, download = self.requests
        self.assertEqual(api.full_url, "https://api.github.com/repos/x/y")
        # Unredirected: urllib doesn't copy it to a redirect, which may go to another host.
        self.assertEqual(api.unredirected_hdrs, {"Authorization": "Bearer secret"})
        self.assertNotIn("Authorization", api.headers)
        self.assertIsNone(download.get_header("Authorization"))

    def test_not_found(self):
        self.answers = [http_error(404)]
        with self.assertRaises(net.NotFound):
            self.gh.api("/repos/x/y")

    def test_retries_server_errors(self):
        self.answers = [http_error(502), urllib.error.URLError("reset"), b"[]"]
        self.assertEqual(self.gh.api("/x"), [])
        self.assertEqual(len(self.requests), 3)

    def test_retries_cut_off_bodies(self):
        class Cut(Response):
            def read(self, *_):
                raise http.client.IncompleteRead(b"par")

        class Reset(Response):
            def read(self, *_):
                raise ConnectionResetError("reset")

        self.answers = [Cut(), Reset(), b"zip"]
        self.assertEqual(self.gh.download("https://x/y.zip"), b"zip")

    def test_retries_bodies_shorter_than_content_length(self):
        short = Response(b"zi")
        short.headers = {"Content-Length": "3"}
        self.answers = [short, b"zip"]
        self.assertEqual(self.gh.download("https://x/y.zip"), b"zip")
        self.assertEqual(len(self.requests), 2)

    def test_bad_json(self):
        self.answers = [b"<html>"]
        with self.assertRaisesRegex(model.Error, "not JSON"):
            self.gh.api("/x")

    def test_gives_up_after_three_tries(self):
        self.answers = [http_error(503)] * 3
        with self.assertRaisesRegex(model.Error, "HTTP 503"):
            self.gh.api("/x")

    def test_client_errors_are_not_retried(self):
        self.answers = [http_error(401)]
        with self.assertRaisesRegex(model.Error, "HTTP 401"):
            self.gh.api("/x")
        self.assertEqual(len(self.requests), 1)

    def test_pages(self):
        self.answers = [json.dumps(list(range(100))).encode(), b"[100]"]
        self.assertEqual(self.gh.api_pages("/tags"), list(range(101)))
        self.assertTrue(self.requests[1].full_url.endswith("/tags?per_page=100&page=2"))

    def test_raw_missing_file_is_none(self):
        self.answers = [http_error(404)]
        self.assertIsNone(self.gh.raw("x/y", "c" * 40, "src/lib.rs"))
        self.assertIsNone(self.gh.raw("x/y", "c" * 40, "src/lib.rs"))  # cached
        self.assertEqual(len(self.requests), 1)

    def test_verify_attestation_runs_gh(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            out = json.dumps([{"verificationResult": {}}]) if "--repo" in cmd else ""
            return subprocess.CompletedProcess(cmd, 0, out, "")

        with mock.patch("shutil.which", return_value="/usr/bin/gh"), \
                mock.patch("subprocess.run", run):
            self.assertEqual(self.gh.verify_attestation(b"zip", "x/y"), [{"verificationResult": {}}])
        self.assertEqual(calls[1][:3], ["gh", "attestation", "verify"])
        self.assertEqual(calls[1][4:], ["--repo", "x/y", "--format", "json"])

    def test_verify_attestation_needs_gh(self):
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaisesRegex(model.Error, "gh` 2.49"):
                self.gh.verify_attestation(b"zip", "x/y")


if __name__ == "__main__":
    unittest.main()
