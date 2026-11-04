"""Writes the TOML files of the source of truth in one fixed layout, so a PR's diff is small and
looks the same every time. Only the value types those files use are supported."""

from __future__ import annotations

import json


def string(s: str) -> str:
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
    raise TypeError(f"can't write {type(v).__name__} to TOML")


def document(top: dict, tables: dict[str, dict | None], header: str = "") -> str:
    """`top` key/values first, then one `[table]` per entry of `tables` (skipping None)."""
    lines = [f"# {line}".rstrip() for line in header.splitlines()] if header else []
    if lines:
        lines.append("")
    lines += [f"{k} = {value(v)}" for k, v in top.items()]
    for name, table in tables.items():
        if table is None:
            continue
        lines += ["", f"[{name}]"]
        lines += [f"{k} = {value(v)}" for k, v in table.items()]
    return "\n".join(lines) + "\n"


def array_of_tables(name: str, entries: list[dict], header: str = "") -> str:
    lines = [f"# {line}".rstrip() for line in header.splitlines()] if header else []
    for entry in entries:
        if lines:
            lines.append("")
        lines.append(f"[[{name}]]")
        lines += [f"{k} = {value(v)}" for k, v in entry.items()]
    return "\n".join(lines) + "\n"


def version_file(v: dict) -> str:
    top = {k: v[k] for k in ("version", "channel", "tag", "published", "deps", "platforms")}
    return document(top, {
        "source": v["source"],
        "weft": v["weft"],
        "asset": v["asset"],
        "provenance": v.get("provenance"),
        "reviewed": v["reviewed"],
    })


def mod_file(meta: dict) -> str:
    top = {k: meta[k] for k in ("name", "summary", "authors", "license", "homepage", "icon")
           if meta.get(k) is not None}
    return document(top, {})
