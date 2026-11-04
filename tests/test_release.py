"""Offline tests of the parts of `add-release` that read release notes, zips and mod sources."""

import io
import struct
import unittest
import zipfile

from weftindex import release
from weftindex.release import ReleaseError


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def pe(machine=0x8664) -> bytes:
    head = bytearray(0x200)
    head[:2] = b"MZ"
    struct.pack_into("<I", head, 0x3C, 0x80)
    head[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", head, 0x84, machine)
    return bytes(head)


def elf(machine=62) -> bytes:
    head = bytearray(0x40)
    head[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", head, 0x12, machine)
    return bytes(head)


def macho(cpu=0x0100000C) -> bytes:
    return b"\xcf\xfa\xed\xfe" + struct.pack("<I", cpu) + bytes(24)


def fat_macho(*cpus) -> bytes:
    out = b"\xca\xfe\xba\xbe" + struct.pack(">I", len(cpus))
    for cpu in cpus:
        out += struct.pack(">IIIII", cpu, 0, 0, 0, 0)
    return out


class ZipPlatformsTest(unittest.TestCase):
    def test_all_three(self):
        data = _zip({"cogwork.dll": pe(), "libcogwork.so": elf(), "libcogwork.dylib": macho(),
                     "assets/lang/en_us.lang": b"x"})
        self.assertEqual(release.zip_platforms(data, "cogwork"),
                         ["windows-x64", "linux-x64", "macos-arm64"])

    def test_universal_dylib(self):
        data = _zip({"libcogwork.dylib": fat_macho(0x01000007, 0x0100000C)})
        self.assertEqual(release.zip_platforms(data, "cogwork"), ["macos-arm64"])

    def test_wrong_architecture_is_an_error(self):
        with self.assertRaises(ReleaseError):
            release.zip_platforms(_zip({"cogwork.dll": pe(machine=0xAA64)}), "cogwork")
        with self.assertRaises(ReleaseError):
            release.zip_platforms(_zip({"libcogwork.dylib": macho(cpu=0x01000007)}), "cogwork")

    def test_library_must_be_named_after_the_mod(self):
        with self.assertRaises(ReleaseError):
            release.zip_platforms(_zip({"other.dll": pe()}), "cogwork")

    def test_nested_library_does_not_count(self):
        with self.assertRaises(ReleaseError):
            release.zip_platforms(_zip({"sub/cogwork.dll": pe()}), "cogwork")

    def test_not_a_zip(self):
        with self.assertRaises(ReleaseError):
            release.zip_platforms(b"not a zip", "cogwork")


class NotesTest(unittest.TestCase):
    BODY = ("A release candidate, built from `main`.\n\nBuilt against Weft 26.11.1, commit "
            "[ff9c712](https://github.com/Weft-Rust/Weft/commit/"
            "ff9c7128511fc0dce7719b67c9a59274d70bdd5a).\n\nPut `cogwork.zip` ...")

    def test_parses_built_against(self):
        m = release.NOTES_RE.search(self.BODY)
        self.assertEqual(m.group("version"), "26.11.1")
        self.assertEqual(m.group("short"), "ff9c712")
        self.assertEqual(m.group("repo"), "Weft-Rust/Weft")
        self.assertEqual(m.group("commit"), "ff9c7128511fc0dce7719b67c9a59274d70bdd5a")

    def test_no_line(self):
        self.assertIsNone(release.NOTES_RE.search("Just a release."))


class DescribeTest(unittest.TestCase):
    TAGS = {"v26.11.1": "f" * 40, "v26.10.4": "3" * 40, "v0.1.0": "5" * 40}

    def test_exact_tag(self):
        self.assertEqual(release.describe("3" * 40, self.TAGS, lambda t, c: None), "v26.10.4")

    def test_nearest_ancestor(self):
        ahead = {"v26.11.1": 46, "v26.10.4": 120, "v0.1.0": 400}
        commit = "4563878b067425085701268f074f9483b2625e4c"
        got = release.describe(commit, self.TAGS, lambda t, c: ahead[t])
        self.assertEqual(got, "v26.11.1-46-g4563878")

    def test_skips_tags_that_are_not_ancestors(self):
        ahead = {"v26.11.1": None, "v26.10.4": 56, "v0.1.0": 300}
        got = release.describe("5faf2e3" + "0" * 33, self.TAGS, lambda t, c: ahead[t])
        self.assertEqual(got, "v26.10.4-56-g5faf2e3")

    def test_no_ancestor(self):
        with self.assertRaises(ReleaseError):
            release.describe("a" * 40, self.TAGS, lambda t, c: None)


class SourceMetadataTest(unittest.TestCase):
    def test_parse_lang(self):
        lang = release.parse_lang("# comment\nmod.x.name = X\nmod.x.description = A = B\n\n")
        self.assertEqual(lang, {"mod.x.name": "X", "mod.x.description": "A = B"})

    def test_summary_first_sentence(self):
        self.assertEqual(release.summarize("Does a thing. Then more.\\n\\nSecond paragraph."),
                         "Does a thing.")

    def test_summary_strips_markup(self):
        self.assertEqual(release.summarize("Type {#a0a0a0}/play{/} to play."), "Type /play to play.")

    def test_summary_shortened(self):
        text = ("Gears, item tubes and engines, an excavator that digs out a whole area layer by "
                "layer, an assembly bench that crafts on its own and a leveler that makes land flat.")
        s = release.summarize(text)
        self.assertLessEqual(len(s), 140)
        self.assertTrue(s.endswith("…"))
        self.assertEqual(s, "Gears, item tubes and engines, an excavator that digs out a whole area "
                            "layer by layer…")

    def test_summary_shortened_without_commas(self):
        s = release.summarize("word " * 60)
        self.assertLessEqual(len(s), 140)
        self.assertTrue(s.endswith("word…"))

    def test_deps_plain(self):
        self.assertEqual(release.parse_deps('declare_mod!(deps: ["base"], init: init);'), ["base"])
        self.assertEqual(release.parse_deps("declare_mod!(deps: [], init: init);"), [])

    def test_deps_feature_gated(self):
        src = ('#[cfg(not(feature = "showcase"))]\ndeclare_mod!(deps: ["base"], init: init);\n'
               '#[cfg(feature = "showcase")]\ndeclare_mod!(deps: ["base", "showcase"], init: init);')
        self.assertEqual(release.parse_deps(src), ["base"])
        swapped = ('#[cfg(feature = "showcase")]\ndeclare_mod!(deps: ["base", "showcase"], init: init);\n'
                   '#[cfg(not(feature = "showcase"))]\ndeclare_mod!(deps: ["base"], init: init);')
        self.assertEqual(release.parse_deps(swapped), ["base"])

    def test_deps_missing(self):
        with self.assertRaises(ReleaseError):
            release.parse_deps("fn main() {}")


if __name__ == "__main__":
    unittest.main()
