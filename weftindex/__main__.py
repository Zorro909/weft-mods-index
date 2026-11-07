"""python3 -m weftindex <command>; `--help` lists the commands, README.md explains them."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from . import model

ROOT = Path(__file__).resolve().parent.parent


def _github():
    from .net import GitHub
    return GitHub()


def cmd_validate(args) -> int:
    model.load_valid(args.root)
    print("valid")
    return 0


def cmd_build(args) -> int:
    from .release import build
    data = build(_github(), args.root)
    out = (args.out or args.root / "dist") / "v2" / "index.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    index = json.loads(data)
    print(f"\nwrote {out}: {len(index['mods'])} mods, "
          f"{sum(len(m['builds']) for m in index['mods'])} builds, "
          f"{len(index['weft'])} Weft releases, {len(index['revoked'])} revoked")
    return 0


def cmd_add_release(args) -> int:
    from .release import add_release
    _, title, body = add_release(_github(), args.root, args.repo, args.tags, args.summary)
    dist = args.root / "dist"
    dist.mkdir(exist_ok=True)
    (dist / "pr.md").write_text(body, encoding="utf-8")
    (dist / "pr-title.txt").write_text(title + "\n", encoding="utf-8")
    print(f"\nvalid. Now commit, push and open the pull request:\n\n"
          f"  git add mods && git commit -m {shlex.quote(title)}\n"
          f"  git push -u origin HEAD\n"
          f"  gh pr create --title {shlex.quote(title)} --body-file dist/pr.md\n")
    return 0


def cmd_sync_weft(args) -> int:
    from .weft import sync_weft
    added = sync_weft(_github(), args.root)
    print(f"added {', '.join(added)}" if added else "weft.toml is up to date")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m weftindex")
    p.add_argument("--root", type=Path, default=ROOT, help="the index repository")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("add-release", help="verify GitHub releases and write their version files")
    a.add_argument("repo", help="owner/name of the mod's GitHub repo")
    a.add_argument("tags", nargs="+", metavar="tag", help="<id>-v<version>")
    a.add_argument("--summary", help="one line for a mod new to the index (mod.toml)")
    a.set_defaults(func=cmd_add_release)

    sub.add_parser("validate", help="check every file, offline").set_defaults(func=cmd_validate)

    b = sub.add_parser("build", help="verify every release and write <out>/v2/index.json")
    b.add_argument("--out", type=Path, help="default: <root>/dist")
    b.set_defaults(func=cmd_build)

    s = sub.add_parser("sync-weft", help="add new Weft releases and their ABI to weft.toml")
    s.set_defaults(func=cmd_sync_weft)

    args = p.parse_args(argv)
    args.root = args.root.resolve()
    try:
        return args.func(args)
    except model.ValidationError as e:
        for err in e.errors:
            print(f"error: {err}", file=sys.stderr)
        print(f"{len(e.errors)} error(s)", file=sys.stderr)
    except model.Error as e:
        print(f"error: {e}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
