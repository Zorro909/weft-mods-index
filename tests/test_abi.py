import unittest

from weftindex.abi import abi_hash, fnv1a64


class AbiHashTest(unittest.TestCase):
    def test_reference_vectors(self):
        # Published FNV-1a-64 test vectors.
        self.assertEqual(fnv1a64(b""), 0xCBF29CE484222325)
        self.assertEqual(fnv1a64(b"a"), 0xAF63DC4C8601EC8C)
        self.assertEqual(fnv1a64(b"foobar"), 0x85944171F73967E8)

    def test_format_is_16_lowercase_hex(self):
        self.assertEqual(abi_hash(b""), "cbf29ce484222325")
        h = abi_hash(b"pub struct ModDecl;\n")
        self.assertRegex(h, r"^[0-9a-f]{16}$")

    def test_leading_zeros_are_kept(self):
        # Find an input whose hash starts with a zero nibble, and check it is padded.
        for i in range(10_000):
            data = str(i).encode()
            if fnv1a64(data) < 1 << 60:
                self.assertEqual(len(abi_hash(data)), 16)
                self.assertTrue(abi_hash(data).startswith("0"))
                return
        self.fail("no input with a leading zero found")

    def test_raw_bytes_not_text(self):
        # The engine hashes bytes as they are: CRLF and LF differ.
        self.assertNotEqual(abi_hash(b"a\n"), abi_hash(b"a\r\n"))


if __name__ == "__main__":
    unittest.main()
