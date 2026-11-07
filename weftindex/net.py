"""Everything that talks to GitHub: its REST API, public downloads, and `gh attestation verify`.
Standard library only. The API token (GH_TOKEN, GITHUB_TOKEN, or `gh auth token`) is only sent with
requests to api.github.com, as an unredirected header: urllib drops it from any redirect.

Tests replace `GitHub` with a fake that has the same five methods."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from .model import Error

API = "https://api.github.com"
USER_AGENT = "weft-mods-index"
# A mod zip is a few MB; refuse anything absurd rather than fill the disk or memory.
MAX_DOWNLOAD = 512 * 1024 * 1024
RETRIES = 3


class NotFound(Error):
    pass


def _token() -> str | None:
    for var in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    if shutil.which("gh"):
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return None


class GitHub:
    def __init__(self, token: str | None = ...):
        self.token = _token() if token is ... else token
        self._raw_cache: dict[str, bytes | None] = {}

    def _get(self, url: str, headers: dict, limit: int, token: str | None = None) -> bytes:
        """The body at `url`, retrying server errors, dropped connections and cut-off bodies."""
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
        if token:
            req.add_unredirected_header("Authorization", f"Bearer {token}")
        for attempt in range(RETRIES):
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    data = r.read(limit + 1)
                    length = r.headers.get("Content-Length")
                if length and length.isdigit() and len(data) < int(length):
                    raise http.client.IncompleteRead(data, int(length) - len(data))
                return data
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise NotFound(f"GET {url}: not found") from None
                if e.code < 500 or attempt == RETRIES - 1:
                    raise Error(f"GET {url}: HTTP {e.code} {e.reason}") from None
            except (OSError, http.client.HTTPException) as e:  # URLError, resets, timeouts
                if attempt == RETRIES - 1:
                    raise Error(f"GET {url}: {e}") from None
            time.sleep(2 ** attempt)
        raise AssertionError("unreachable")

    def api(self, path: str):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        body = self._get(API + path, headers, MAX_DOWNLOAD, self.token)
        try:
            return json.loads(body)
        except ValueError:
            raise Error(f"GET {API + path}: the response is not JSON") from None

    def api_pages(self, path: str) -> list:
        out, page = [], 1
        sep = "&" if "?" in path else "?"
        while True:
            batch = self.api(f"{path}{sep}per_page=100&page={page}")
            out += batch
            if len(batch) < 100:
                return out
            page += 1

    def download(self, url: str) -> bytes:
        """The bytes at a public URL, without credentials."""
        data = self._get(url, {}, MAX_DOWNLOAD)
        if len(data) > MAX_DOWNLOAD:
            raise Error(f"{url} is larger than {MAX_DOWNLOAD} bytes")
        return data

    def raw(self, repo: str, commit: str, path: str) -> bytes | None:
        """A file of a public repo at a commit, or None if it doesn't exist there."""
        url = f"https://raw.githubusercontent.com/{repo}/{commit}/{path}"
        if url not in self._raw_cache:
            try:
                self._raw_cache[url] = self.download(url)
            except NotFound:
                self._raw_cache[url] = None
        return self._raw_cache[url]

    def verify_attestation(self, data: bytes, bundle: dict, repo: str) -> list[dict]:
        """`gh attestation verify --bundle <bundle> --repo <repo>` of the bytes: the JSON results,
        or an Error if the bundle doesn't verify for them."""
        if not shutil.which("gh") or subprocess.run(["gh", "attestation", "verify", "--help"],
                                                    capture_output=True).returncode != 0:
            raise Error("verifying an attestation needs `gh` 2.49 or newer")
        with tempfile.TemporaryDirectory() as tmp:
            path, bundle_path = Path(tmp) / "mod.zip", Path(tmp) / "bundle.json"
            path.write_bytes(data)
            bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
            r = subprocess.run(["gh", "attestation", "verify", str(path), "--bundle",
                                str(bundle_path), "--repo", repo, "--format", "json"],
                               capture_output=True, text=True)
        if r.returncode != 0:
            raise Error(f"gh attestation verify failed:\n{r.stderr.strip()}")
        try:
            return json.loads(r.stdout)
        except ValueError:
            raise Error("gh attestation verify printed no JSON") from None


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
