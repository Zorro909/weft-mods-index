"""Writes the TOML files of the repository in one fixed layout, so a PR's diff is small and looks
the same every time. Only strings are supported: they are all these files hold."""

from __future__ import annotations

import json

from .model import Error


def string(s: str) -> str:
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate, as JSON's "\ud800" can give
        raise Error("not valid Unicode") from None
    # A JSON string is a valid TOML basic string, except that TOML also forbids a raw DEL.
    return json.dumps(s, ensure_ascii=False).replace("\x7f", "\\u007f")


def _lines(table: dict) -> list[str]:
    return [f"{k} = {string(v)}" for k, v in table.items()]


def document(table: dict) -> str:
    return "\n".join(_lines(table)) + "\n"


def array_of_tables(name: str, entries: list[dict], header: str = "") -> str:
    lines = [f"# {line}".rstrip() for line in header.splitlines()]
    for entry in entries:
        lines += ([""] if lines else []) + [f"[[{name}]]", *_lines(entry)]
    return "\n".join(lines) + "\n"
