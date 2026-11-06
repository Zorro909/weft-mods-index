"""Which platforms a mod zip has a library for, from the libraries' file names and headers."""

from __future__ import annotations

import struct
import zipfile
from io import BytesIO

from . import model


class PlatformError(model.Error):
    pass


def _pe_platform(head: bytes) -> str | None:
    if head[:2] != b"MZ":
        return None
    off = struct.unpack_from("<I", head, 0x3C)[0]
    if head[off:off + 4] != b"PE\0\0":
        return None
    return "windows-x64" if struct.unpack_from("<H", head, off + 4)[0] == 0x8664 else None


def _elf_platform(head: bytes) -> str | None:
    if head[:6] != b"\x7fELF\x02\x01":  # 64-bit, little-endian
        return None
    return "linux-x64" if struct.unpack_from("<H", head, 0x12)[0] == 62 else None


def _macho_platform(head: bytes) -> str | None:
    arm64 = 0x0100000C
    if head[:4] == b"\xcf\xfa\xed\xfe":
        return "macos-arm64" if struct.unpack_from("<I", head, 4)[0] == arm64 else None
    if head[:4] == b"\xca\xfe\xba\xbe":  # universal binary
        n = struct.unpack_from(">I", head, 4)[0]
        if any(struct.unpack_from(">I", head, 8 + 20 * i)[0] == arm64 for i in range(min(n, 16))):
            return "macos-arm64"
    return None


def zip_platforms(data: bytes, mod_id: str) -> list[str]:
    """The platforms whose library is at the top of the zip, by its file name and its header."""
    found = set()
    checks = {f"{mod_id}.dll": _pe_platform, f"lib{mod_id}.so": _elf_platform,
              f"lib{mod_id}.dylib": _macho_platform}
    try:
        with zipfile.ZipFile(BytesIO(data)) as z:
            for name, check in checks.items():
                if name not in z.namelist():
                    continue
                with z.open(name) as f:
                    head = f.read(4096)
                try:
                    platform = check(head)
                except struct.error:  # a header cut short
                    platform = None
                if platform is None:
                    raise PlatformError(f"{name} in the zip is not a library for a known platform")
                found.add(platform)
    except zipfile.BadZipFile as e:
        raise PlatformError(f"{mod_id}.zip is not a zip: {e}") from None
    if not found:
        raise PlatformError(f"{mod_id}.zip holds no library named after {mod_id}")
    return [p for p in model.PLATFORMS if p in found]
