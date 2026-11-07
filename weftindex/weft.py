"""Weft's release tags and the mod ABI of each: weft.toml, which becomes the index's `weft` map.
The launcher takes the ABI of the player's Weft version from that map."""

from __future__ import annotations

from pathlib import Path

from . import model, tomlw
from .abi import ABI_FILE, abi_hash
from .net import GitHub

WEFT_REPO = "Weft-Rust/Weft"


class Weft:
    """Weft's release tags, and the ABI of its commits, each fetched once."""

    def __init__(self, gh: GitHub):
        self.gh = gh
        self._tags: dict[str, str] | None = None
        self._abi: dict[str, str] = {}

    @property
    def tags(self) -> dict[str, str]:
        """Every release tag, and its commit."""
        if self._tags is None:
            self._tags = {t["name"]: t["commit"]["sha"]
                          for t in self.gh.api_pages(f"/repos/{WEFT_REPO}/tags")
                          if model.WEFT_TAG_RE.fullmatch(t["name"])}
        return self._tags

    def abi(self, commit: str) -> str:
        if commit not in self._abi:
            src = self.gh.raw(WEFT_REPO, commit, ABI_FILE)
            if src is None:
                raise model.Error(f"{WEFT_REPO} has no {ABI_FILE} at {commit}")
            self._abi[commit] = abi_hash(src)
        return self._abi[commit]

    def release(self, tag: str) -> dict:
        """The weft.toml entry of a release tag."""
        return {"tag": tag, "commit": self.tags[tag], "abi": self.abi(self.tags[tag])}


def verify(gh: GitHub, releases: list[dict]) -> list[str]:
    """What is wrong with weft.toml: a tag that moved or is gone, or an ABI that isn't the hash
    of the commit's ABI file."""
    weft, errors = Weft(gh), []
    for rel in releases:
        try:
            if rel["tag"] not in weft.tags:
                errors.append(f"weft.toml: {WEFT_REPO} has no tag {rel['tag']}")
            elif (now := weft.release(rel["tag"])) != rel:
                errors.append(f"weft.toml: {rel['tag']} is {now['commit']} with ABI {now['abi']}, "
                              f"not {rel['commit']} with ABI {rel['abi']}")
        except model.Error as e:
            errors.append(f"weft.toml: {rel['tag']}: {e}")
    return errors


def weft_file(entries: list[dict]) -> str:
    entries = sorted(entries, key=lambda r: model.weft_tag_key(r["tag"]), reverse=True)
    return tomlw.array_of_tables(
        "release", [{"tag": r["tag"], "commit": r["commit"], "abi": r["abi"]} for r in entries],
        "Weft releases and the mod ABI hash of each: FNV-1a-64 of crates/abi/src/lib.rs at the "
        "commit.\nBecomes the index's `weft` map. `python3 -m weftindex sync-weft` adds new "
        "releases.")


def sync_weft(gh: GitHub, root: Path) -> list[str]:
    """Adds every Weft release missing from weft.toml. Returns the tags added."""
    repo = model.load_valid(root)
    weft = Weft(gh)
    have = {r["tag"] for r in repo.weft}
    added = [t for t in weft.tags if t not in have]
    if added:
        (root / "weft.toml").write_text(weft_file(repo.weft + [weft.release(t) for t in added]),
                                         encoding="utf-8")
    return added
