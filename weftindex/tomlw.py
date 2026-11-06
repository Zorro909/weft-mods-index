"""Writes the TOML files of the source of truth in one fixed layout, so a PR's diff is small and
looks the same every time. Only the value types those files use are supported."""

from __future__ import annotations

import json

from .model import MOD_FIELDS, VERSION_FIELDS, Error


def string(s: str) -> str:
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate, as JSON's "\ud800" can give
        raise Error("not valid Unicode") from None
    # A JSON string is a valid TOML basic string, except that TOML also forbids a raw DEL.
    return json.dumps(s, ensure_ascii=False).replace("\x7f", "\\u007f")


def value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return string(v)
    if isinstance(v, list):
        return "[" + ", ".join(value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {value(x)}" for k, x in v.items()) + " }"
    raise Error(f"can't write a {type(v).__name__} to TOML")


def _header(header: str) -> list[str]:
    return [f"# {line}".rstrip() for line in header.splitlines()]


def _line(key: str, v, where: str | None = None) -> str:
    try:
        return f"{key} = {value(v)}"
    except Error as e:
        raise Error(f"{where or key}: {e}") from None


def document(top: dict, tables: dict[str, dict | None], header: str = "") -> str:
    """`top` key/values first, then one `[table]` per entry of `tables` (skipping None)."""
    lines = _header(header) + [""] if header else []
    lines += [_line(k, v) for k, v in top.items()]
    for name, table in tables.items():
        if table is not None:
            lines += ["", f"[{name}]", *(_line(k, v, f"{name}.{k}") for k, v in table.items())]
    return "\n".join(lines) + "\n"


def array_of_tables(name: str, entries: list[dict], header: str = "") -> str:
    lines = _header(header)
    for entry in entries:
        lines += ([""] if lines else []) + [f"[[{name}]]", *(_line(k, v) for k, v in entry.items())]
    return "\n".join(lines) + "\n"


def version_file(v: dict) -> str:
    """The plain values first, then one table each, in VERSION_FIELDS order."""
    return document({k: v[k] for k, keys in VERSION_FIELDS.items() if keys is None},
                    {k: v.get(k) for k, keys in VERSION_FIELDS.items() if keys is not None})


def mod_file(meta: dict) -> str:
    return document({k: meta[k] for k in MOD_FIELDS if meta.get(k) is not None}, {})
