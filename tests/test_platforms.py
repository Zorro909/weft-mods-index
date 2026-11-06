"""Offline tests of reading the libraries in a zip (platforms.py)."""

import unittest

from tests.fixtures import fat_macho, elf, macho, pe, zip_of as _zip
from weftindex import platforms
from weftindex.model import Error as ReleaseError


class ZipPlatformsTest(unittest.TestCase):
    def test_all_three(self):
        data = _zip({"cogwork.dll": pe(), "libcogwork.so": elf(), "libcogwork.dylib": macho(),
                     "assets/lang/en_us.lang": b"x"})
        self.assertEqual(platforms.zip_platforms(data, "cogwork"),
                         ["windows-x64", "linux-x64", "macos-arm64"])

    def test_universal_dylib(self):
        data = _zip({"libcogwork.dylib": fat_macho(0x01000007, 0x0100000C)})
        self.assertEqual(platforms.zip_platforms(data, "cogwork"), ["macos-arm64"])

    def test_wrong_architecture_is_an_error(self):
        with self.assertRaises(ReleaseError):
            platforms.zip_platforms(_zip({"cogwork.dll": pe(machine=0xAA64)}), "cogwork")
        with self.assertRaises(ReleaseError):
            platforms.zip_platforms(_zip({"libcogwork.dylib": macho(cpu=0x01000007)}), "cogwork")

    def test_library_must_be_named_after_the_mod(self):
        with self.assertRaises(ReleaseError):
            platforms.zip_platforms(_zip({"other.dll": pe()}), "cogwork")

    def test_nested_library_does_not_count(self):
        with self.assertRaises(ReleaseError):
            platforms.zip_platforms(_zip({"sub/cogwork.dll": pe()}), "cogwork")

    def test_not_a_zip(self):
        with self.assertRaises(ReleaseError):
            platforms.zip_platforms(b"not a zip", "cogwork")


    def test_truncated_header(self):
        with self.assertRaises(ReleaseError):
            platforms.zip_platforms(_zip({"cogwork.dll": b"MZ"}), "cogwork")


if __name__ == "__main__":
    unittest.main()
