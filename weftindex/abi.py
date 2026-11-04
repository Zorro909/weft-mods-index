"""The Weft mod ABI hash.

The engine loads a mod only if the mod was built against the same `crates/abi/src/lib.rs`: its
`ABI_VERSION` is the FNV-1a-64 hash of that file's raw bytes. We print it as 16 lowercase hex
characters, as the index does.
"""

ABI_FILE = "crates/abi/src/lib.rs"

FNV_OFFSET = 0xCBF29CE484222325
FNV_PRIME = 0x100000001B3
_MASK = 0xFFFFFFFFFFFFFFFF


def fnv1a64(data: bytes) -> int:
    h = FNV_OFFSET
    for byte in data:
        h ^= byte
        h = (h * FNV_PRIME) & _MASK
    return h


def abi_hash(abi_source: bytes) -> str:
    """The ABI hash of the bytes of `crates/abi/src/lib.rs`, as 16 lowercase hex characters."""
    return f"{fnv1a64(abi_source):016x}"
