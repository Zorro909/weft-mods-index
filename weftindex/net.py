"""HTTP access to GitHub, stdlib only. The API token (GH_TOKEN, GITHUB_TOKEN, or `gh auth token`)
is only ever sent to api.github.com, never to the download hosts a release asset redirects to."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request

API = "https://api.github.com"
USER_AGENT = "weft-mods-index"
# A mod zip is a few MB; refuse anything absurd rather than fill the disk or memory.
MAX_DOWNLOAD = 512 * 1024 * 1024


class NotFound(Exception):
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

    def _open(self, url: str, headers: dict):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
        try:
            return urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(url) from None
            raise RuntimeError(f"GET {url}: HTTP {e.code} {e.reason}") from None

    def api(self, path: str):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        with self._open(API + path, headers) as r:
            return json.load(r)

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
        with self._open(url, {}) as r:
            data = r.read(MAX_DOWNLOAD + 1)
        if len(data) > MAX_DOWNLOAD:
            raise RuntimeError(f"{url} is larger than {MAX_DOWNLOAD} bytes")
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


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
