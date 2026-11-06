import copy
import hashlib
import json
import tomllib
import unittest
from pathlib import Path

from tests.fixtures import ABI, WEFT, scratch_repo
from weftindex import model, tomlw

REPO = "Zorro909/weft-mods"
C1 = "1" * 40
WEFT2 = "4563878b067425085701268f074f9483b2625e4c"
GEN = "2026-11-04T18:00:00Z"


def version(mod_id="cogwork", ver="0.2.0", **over):
    tag = f"{mod_id}-v{ver}"
    v = {
        "version": ver,
        "channel": model.channel_for(ver),
        "tag": tag,
        "published": "2026-11-04T02:39:03Z",
        "deps": [{"id": "base"}],
        "platforms": ["windows-x64", "linux-x64", "macos-arm64"],
        "source": {"repo": REPO, "commit": C1},
        "weft": {"tag": "v26.11.1", "commit": WEFT, "abi": ABI},
        "asset": {"name": f"{mod_id}.zip", "url": model.expected_asset_url(REPO, tag, mod_id),
                  "size": 100, "sha256": hashlib.sha256(tag.encode()).hexdigest()},
        "provenance": None,
        "reviewed": {"by": "Zorro909", "at": "2026-11-04"},
    }
    for k, val in over.items():
        v[k] = val
    return v


META = {"name": "Cogwork", "summary": "Gears and tubes.", "authors": ["Zorro909"],
        "homepage": "https://github.com/Zorro909/weft-mods/tree/main/cogwork"}


class Fixture:
    """A temporary index repository."""

    def __init__(self, test):
        self.root = scratch_repo(test, [
            {"tag": "v26.11.1", "commit": WEFT, "abi": ABI},
            {"tag": "v26.10.4", "commit": "34fa22b1b6546aa8a8d3d12252bd7a5b01dbb0ec",
             "abi": "5f0569a6bea71b81"},
        ])

    def mod(self, mod_id="cogwork", meta=META):
        d = self.root / "mods" / mod_id
        (d / "versions").mkdir(parents=True, exist_ok=True)
        (d / "mod.toml").write_text(tomlw.mod_file(meta))

    def add(self, v, mod_id="cogwork", name=None):
        if not (self.root / "mods" / mod_id / "mod.toml").exists():
            self.mod(mod_id)
        p = self.root / "mods" / mod_id / "versions" / f"{name or v['version']}.toml"
        p.write_text(tomlw.version_file(v))
        return p

    def raw_version(self, text, mod_id="cogwork", name="0.2.0"):
        self.mod(mod_id)
        (self.root / "mods" / mod_id / "versions" / f"{name}.toml").write_text(text)

    def revoked(self, entries):
        (self.root / "revoked.toml").write_text(tomlw.array_of_tables("revoked", entries))

    def errors(self):
        repo, errors = model.load(self.root)
        return errors + model.validate(repo)

    def build(self):
        return model.build(self.root, GEN)


class ValidIndexTest(unittest.TestCase):
    def setUp(self):
        self.f = Fixture(self)
        self.f.add(version(ver="0.2.0"))
        self.f.add(version(ver="0.2.1-rc.1", published="2026-11-04T12:08:06Z"))
        self.f.add(version(ver="0.1.2-experimental.4563878.917596d",
                           published="2026-11-03T12:00:56Z",
                           weft={"tag": "v26.11.1-46-g4563878", "commit": WEFT2,
                                 "abi": "5d449b795eda0099"}))
        self.f.revoked([{"sha256": "a" * 64, "reason": "Deletes saves", "at": "2026-11-05"}])

    def test_valid(self):
        self.assertEqual(self.f.errors(), [])

    def test_render_matches_contract(self):
        index = json.loads(self.f.build())
        self.assertEqual(list(index), ["schema", "generated", "weft", "mods", "revoked"])
        self.assertEqual(index["schema"], 1)
        self.assertEqual(index["generated"], GEN)
        self.assertEqual([w["tag"] for w in index["weft"]], ["v26.11.1", "v26.10.4"])
        mod = index["mods"][0]
        self.assertEqual(list(mod), ["id", "name", "summary", "authors", "license", "homepage",
                                     "icon", "versions"])
        self.assertIsNone(mod["license"])
        self.assertIsNone(mod["icon"])
        v = mod["versions"][0]
        self.assertEqual(list(v), ["version", "channel", "tag", "published", "source", "weft",
                                   "deps", "platforms", "asset", "provenance", "reviewed"])
        self.assertIsNone(v["provenance"])
        self.assertEqual(v["asset"]["name"], "cogwork.zip")
        self.assertEqual(index["revoked"], [{"sha256": "a" * 64, "reason": "Deletes saves",
                                             "at": "2026-11-05"}])

    def test_versions_newest_first(self):
        index = json.loads(self.f.build())
        self.assertEqual([v["version"] for v in index["mods"][0]["versions"]],
                         ["0.2.1-rc.1", "0.2.0", "0.1.2-experimental.4563878.917596d"])

    def test_deterministic(self):
        a = self.f.build()
        b = self.f.build()
        self.assertEqual(a, b)
        self.assertTrue(a.endswith(b"}\n"))
        self.assertNotIn(b"\r", a)

    def test_provenance_rendered(self):
        prov = {"kind": "github-attestation", "repo": REPO,
                "workflow": ".github/workflows/release.yml", "ref": "refs/heads/main",
                "verified_at": "2026-11-04T18:00:00Z"}
        self.f.add(version(ver="0.2.0", provenance=prov))
        index = json.loads(self.f.build())
        v = next(v for v in index["mods"][0]["versions"] if v["version"] == "0.2.0")
        self.assertEqual(v["provenance"], prov)

    def test_toml_round_trip(self):
        v = version()
        text = tomlw.version_file(v)
        back = tomllib.loads(text)
        expected = copy.deepcopy(v)
        del expected["provenance"]
        self.assertEqual(back, expected)

    def test_toml_string_escapes(self):
        s = 'quote " backslash \\ tab \t unicode … del \x7f'
        self.assertEqual(tomllib.loads(f"x = {tomlw.string(s)}")["x"], s)


class InvalidIndexTest(unittest.TestCase):
    def setUp(self):
        self.f = Fixture(self)

    def assertError(self, fragment):
        errors = self.f.errors()
        self.assertTrue(any(fragment in e for e in errors), f"no {fragment!r} in {errors}")
        with self.assertRaises(model.ValidationError):
            self.f.build()

    def test_bad_id(self):
        self.f.add(version(mod_id="Cog-Work", ver="0.2.0"), mod_id="Cog-Work")
        self.assertError("does not match ^[a-z0-9_]+$")

    def test_asset_name(self):
        v = version()
        v["asset"]["name"] = "other.zip"
        self.f.add(v)
        self.assertError("asset.name must be cogwork.zip")

    def test_asset_url_other_repo(self):
        v = version()
        v["asset"]["url"] = "https://github.com/evil/repo/releases/download/cogwork-v0.2.0/cogwork.zip"
        self.f.add(v)
        self.assertError("asset.url must start with https://github.com/Zorro909/weft-mods/releases/download/")

    def test_asset_url_other_host(self):
        v = version()
        v["asset"]["url"] = "https://example.com/cogwork.zip"
        self.f.add(v)
        self.assertError("asset.url must start with")

    def test_asset_url_other_tag(self):
        v = version()
        v["asset"]["url"] = model.expected_asset_url(REPO, "cogwork-v0.1.0", "cogwork")
        self.f.add(v)
        self.assertError("asset.url must be")

    def test_sha256_format(self):
        v = version()
        v["asset"]["sha256"] = "ABC"
        self.f.add(v)
        self.assertError("asset.sha256")

    def test_sha256_of_the_wrong_type(self):
        v = version()
        v["asset"]["sha256"] = ["a"]
        self.f.add(v)
        self.assertError("asset.sha256 must be a non-empty string")

    def test_uppercase_sha256(self):
        v = version()
        v["asset"]["sha256"] = "A" * 64
        self.f.add(v)
        self.assertError("asset.sha256")

    def test_duplicate_sha256(self):
        a, b = version(ver="0.2.0"), version(ver="0.2.1")
        b["asset"]["sha256"] = a["asset"]["sha256"]
        self.f.add(a)
        self.f.add(b)
        self.assertError("is also pinned by")

    def test_duplicate_version(self):
        self.f.add(version(ver="0.2.0"))
        self.f.add(version(ver="0.2.0"), name="copy")
        self.assertError("duplicate version 0.2.0")

    def test_file_name_must_match_version(self):
        self.f.add(version(ver="0.2.0"), name="0.2.1")
        self.assertError("file name must be 0.2.0.toml")

    def test_tag_must_match(self):
        self.f.add(version(ver="0.2.0", tag="cogwork-v0.2.1"))
        self.assertError("must be cogwork-v0.2.0")

    def test_channel_rules(self):
        self.f.add(version(ver="0.2.1-rc.1", channel="release"))
        self.assertError("channel must be prerelease for version 0.2.1-rc.1")

    def test_experimental_channel(self):
        self.f.add(version(ver="0.1.2-experimental.4563878.917596d", channel="prerelease"))
        self.assertError("channel must be experimental for version")

    def test_unknown_channel(self):
        self.f.add(version(channel="nightly"))
        self.assertError("channel must be release for version 0.2.0")

    def test_experimental_channel_needs_experimental_version(self):
        self.f.add(version(channel="experimental"))
        self.assertError("channel must be release")

    def test_impossible_dates(self):
        self.f.add(version(published="2026-02-30T12:00:00Z",
                           reviewed={"by": "Zorro909", "at": "2026-13-01"}))
        self.assertError("published '2026-02-30T12:00:00Z' is not a")
        self.assertError("reviewed.at '2026-13-01' is not a")

    def test_trailing_newline_is_not_a_match(self):
        self.f.add(version(source={"repo": REPO, "commit": C1 + "\n"}))
        self.assertError("source.commit")

    def test_reviewed_in_the_future(self):
        self.f.add(version(reviewed={"by": "Zorro909", "at": "2999-01-01"}))
        self.assertError("reviewed.at 2999-01-01 is in the future")

    def test_revoked_version_may_name_a_dropped_weft_release(self):
        v = version(weft={"tag": "v26.9.9", "commit": "9" * 40, "abi": "0" * 16})
        self.f.add(v)
        self.assertError("weft.tag v26.9.9 is not in weft.toml")
        self.f.revoked([{"sha256": v["asset"]["sha256"], "reason": "Weft moved", "at": "2026-11-05"}])
        self.assertEqual(self.f.errors(), [])

    def test_control_files_are_required(self):
        self.f.add(version())
        (self.f.root / "revoked.toml").unlink()
        self.assertError("revoked.toml")

    def test_abi_format(self):
        self.f.add(version(weft={"tag": "v26.11.1", "commit": WEFT, "abi": "F470B1CBB1F9AD91"}))
        self.assertError("weft.abi")

    def test_weft_tag_format(self):
        self.f.add(version(weft={"tag": "26.11.1", "commit": WEFT, "abi": ABI}))
        self.assertError("weft.tag")

    def test_weft_commit_must_match_table(self):
        self.f.add(version(weft={"tag": "v26.11.1", "commit": WEFT2, "abi": ABI}))
        self.assertError("weft.commit differs from weft.toml's v26.11.1")

    def test_weft_abi_must_match_table(self):
        self.f.add(version(weft={"tag": "v26.11.1", "commit": WEFT, "abi": "0" * 16}))
        self.assertError("differs from")

    def test_describe_must_end_in_commit(self):
        self.f.add(version(weft={"tag": "v26.11.1-46-gdeadbee", "commit": WEFT2,
                                 "abi": "5d449b795eda0099"}))
        self.assertError("must end in -g4563878")

    def test_describe_base_must_be_known(self):
        self.f.add(version(weft={"tag": "v99.0.0-1-g4563878", "commit": WEFT2,
                                 "abi": "5d449b795eda0099"}))
        self.assertError("which weft.toml doesn't list")

    def test_summary_length(self):
        self.f.mod(meta={**META, "summary": "x" * 141})
        self.f.add(version())
        self.assertError("more than 140")

    def test_summary_one_line(self):
        self.f.mod(meta={**META, "summary": "a\nb"})
        self.f.add(version())
        self.assertError("summary must be one line")

    def test_icon_must_be_https_png(self):
        self.f.mod(meta={**META, "icon": "http://example.com/icon.png"})
        self.f.add(version())
        self.assertError("icon must be an https URL")

    def test_unknown_key(self):
        v = version()
        self.f.add(v)
        p = self.f.root / "mods/cogwork/versions/0.2.0.toml"
        p.write_text("signature = \"x\"\n" + p.read_text())
        self.assertError("unknown key signature")

    def test_missing_key(self):
        self.f.raw_version('version = "0.2.0"\n')
        self.assertError("missing key tag")

    def test_bad_toml(self):
        self.f.raw_version("version = \n")
        self.assertError("0.2.0.toml")

    def test_platforms(self):
        self.f.add(version(platforms=["linux-x64", "windows-x64"]))
        self.assertError("platforms must be unique and in the order")

    def test_unknown_platform(self):
        self.f.add(version(platforms=["linux-arm64"]))
        self.assertError("platforms must be among")

    def test_self_dep(self):
        self.f.add(version(deps=[{"id": "cogwork"}]))
        self.assertError("can't depend on itself")

    def test_provenance_repo(self):
        self.f.add(version(provenance={"kind": "github-attestation", "repo": "evil/repo",
                                       "workflow": ".github/workflows/release.yml",
                                       "ref": "refs/heads/main",
                                       "verified_at": "2026-11-04T18:00:00Z"}))
        self.assertError("provenance.repo must equal source.repo")

    def test_revoked_sha_format(self):
        self.f.add(version())
        self.f.revoked([{"sha256": "xyz", "reason": "bad", "at": "2026-11-05"}])
        self.assertError("revoked.toml entry #1: sha256")

    def test_revoked_duplicate(self):
        self.f.add(version())
        self.f.revoked([{"sha256": "a" * 64, "reason": "bad", "at": "2026-11-05"}] * 2)
        self.assertError("duplicate sha256")

    def test_revoked_date(self):
        self.f.add(version())
        self.f.revoked([{"sha256": "a" * 64, "reason": "bad", "at": "5 Nov"}])
        self.assertError("revoked.toml entry #1: at")

    def test_mod_without_versions(self):
        self.f.mod()
        self.assertError("no versions")

    def test_stray_file(self):
        self.f.add(version())
        (self.f.root / "mods/cogwork/notes.md").write_text("hi")
        self.assertError("unexpected files")

    def test_generated_format(self):
        self.f.add(version())
        with self.assertRaises(model.ValidationError):
            model.build(self.f.root, "2026-11-04 18:00")


class HelpersTest(unittest.TestCase):
    def test_channel_for(self):
        self.assertEqual(model.channel_for("0.2.0"), "release")
        self.assertEqual(model.channel_for("0.2.1-rc.1"), "prerelease")
        self.assertEqual(model.channel_for("0.1.2-experimental.4563878.917596d"), "experimental")

    def test_experimental_sha_with_leading_zero(self):
        self.assertTrue(model.SEMVER_RE.fullmatch("0.1.0-experimental.0123456.abcdef1"))
        # The exception is only for experimental builds.
        self.assertFalse(model.SEMVER_RE.fullmatch("1.0.0-01"))
        self.assertFalse(model.SEMVER_RE.fullmatch("1.0.0-rc.01"))
        self.assertTrue(model.SEMVER_RE.fullmatch("1.0.0-rc.0.x-y"))
        self.assertEqual(model.channel_for("0.1.0-experimental.0123456.abcdef1"), "experimental")

    def test_split_tag(self):
        self.assertEqual(model.split_tag("cogwork-v0.2.0"), ("cogwork", "0.2.0"))
        self.assertEqual(model.split_tag("my_mod-v1.0.0-rc.1"), ("my_mod", "1.0.0-rc.1"))
        self.assertIsNone(model.split_tag("v0.2.0"))

    def test_semver_order(self):
        ordered = ["0.1.0-experimental.ff9c712.8da6d93", "0.1.0-rc.1", "0.1.0-rc.2",
                   "0.1.0-rc.10", "0.1.0", "0.2.0"]
        self.assertEqual(sorted(reversed(ordered), key=model.semver_key), ordered)

    def test_weft_tag_order(self):
        tags = ["v0.1.0", "v26.10.1", "v26.10.10", "v26.10.4", "v26.11.1"]
        self.assertEqual(sorted(tags, key=model.weft_tag_key),
                         ["v0.1.0", "v26.10.1", "v26.10.4", "v26.10.10", "v26.11.1"])


class RepositoryTest(unittest.TestCase):
    """The committed entries of this repository are valid."""

    def test_repository_is_valid(self):
        root = Path(__file__).resolve().parent.parent
        repo, errors = model.load(root)
        self.assertEqual(errors + model.validate(repo), [])


if __name__ == "__main__":
    unittest.main()
