"""Test data shared by the test modules: zips with library headers, and a scratch index repo."""

import io
import struct
import tempfile
import zipfile
from pathlib import Path

from weftindex import tomlw

WEFT = "ff9c7128511fc0dce7719b67c9a59274d70bdd5a"
ABI = "f470b1cbb1f9ad91"


def zip_of(files: dict[str, bytes]) -> bytes:
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


def scratch_repo(test, weft: list[dict] | None = None) -> Path:
    """An empty index repository that is removed after `test`."""
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    root = Path(tmp.name)
    (root / "weft.toml").write_text(tomlw.array_of_tables("release", weft) if weft else "")
    (root / "revoked.toml").write_text("")
    (root / "mods").mkdir()
    return root
