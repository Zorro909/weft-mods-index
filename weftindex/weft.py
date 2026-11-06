"""Weft's release tags, the mod ABI of its commits, and whether a name is true of a commit."""

from __future__ import annotations

from pathlib import Path

from . import model, tomlw
from .abi import ABI_FILE, abi_hash
from .net import GitHub

WEFT_REPO = "Weft-Rust/Weft"


class WeftError(model.Error):
    pass


def ahead_of(gh: GitHub, repo: str, base: str, head: str) -> int | None:
    """How many commits `head` has that `base` doesn't, or None if `base` is not an ancestor."""
    r = gh.api(f"/repos/{repo}/compare/{base}...{head}")
    return r["ahead_by"] if r["status"] in ("ahead", "identical") else None


class Weft:
    """Weft's release tags, and facts about its commits, each fetched once."""

    def __init__(self, gh: GitHub):
        self.gh = gh
        self._tags: dict[str, str] | None = None
        self._abi: dict[str, str] = {}
        self._ahead: dict[tuple[str, str], int | None] = {}

    @property
    def tags(self) -> dict[str, str]:
        """Every release tag the index accepts, and its commit."""
        if self._tags is None:
            self._tags = {t["name"]: t["commit"]["sha"]
                          for t in self.gh.api_pages(f"/repos/{WEFT_REPO}/tags")
                          if model.WEFT_TAG_RE.fullmatch(t["name"])}
        return self._tags

    def abi(self, commit: str) -> str:
        if commit not in self._abi:
            src = self.gh.raw(WEFT_REPO, commit, ABI_FILE)
            if src is None:
                raise WeftError(f"{WEFT_REPO} has no {ABI_FILE} at {commit}")
            self._abi[commit] = abi_hash(src)
        return self._abi[commit]

    def ahead(self, tag: str, commit: str) -> int | None:
        if (tag, commit) not in self._ahead:
            self._ahead[tag, commit] = ahead_of(self.gh, WEFT_REPO, tag, commit)
        return self._ahead[tag, commit]

    def names(self, name: str, commit: str) -> bool:
        """Whether `name` is a release tag of `commit`, or a `git describe` name `<tag>-<n>-g<sha7>`
        that is true of it. Such a name stays true when Weft adds tags later."""
        if name in self.tags:
            return self.tags[name] == commit
        m = model.DESCRIBE_RE.fullmatch(name)
        return bool(m) and m.group(1) in self.tags and m.group(3) == commit[:7] \
            and self.ahead(m.group(1), commit) == int(m.group(2))

    def release(self, tag: str) -> dict:
        """The weft.toml entry of a release tag."""
        return {"tag": tag, "commit": self.tags[tag], "abi": self.abi(self.tags[tag])}


def weft_file(entries: list[dict]) -> str:
    entries = sorted(entries, key=lambda r: model.weft_tag_key(r["tag"]), reverse=True)
    return tomlw.array_of_tables(
        "release", [{"tag": r["tag"], "commit": r["commit"], "abi": r["abi"]} for r in entries],
        "Weft releases and the mod ABI hash of each: FNV-1a-64 of crates/abi/src/lib.rs at the "
        "commit.\nBecomes the index's `weft[]`. `python3 -m weftindex sync-weft` adds new releases.")


def sync_weft(gh: GitHub, root: Path) -> list[str]:
    """Adds every Weft release missing from weft.toml. Returns the tags added."""
    repo, errors = model.load(root)
    if errors:
        raise model.ValidationError(errors)
    weft = Weft(gh)
    have = {r["tag"] for r in repo.weft}
    added = [t for t in weft.tags if t not in have]
    if added:
        (root / "weft.toml").write_text(weft_file(repo.weft + [weft.release(t) for t in added]),
                                         encoding="utf-8")
    return added
