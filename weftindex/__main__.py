"""python3 -m weftindex <command>; `--help` lists the commands, README.md explains them."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from . import model

ROOT = Path(__file__).resolve().parent.parent


def _generated(root: Path) -> str:
    """The `generated` time: SOURCE_DATE_EPOCH, else the time of the last commit, so building the
    same commit twice gives the same bytes."""
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if not epoch:
        r = subprocess.run(["git", "log", "-1", "--format=%ct"], cwd=root, capture_output=True,
                           text=True)
        epoch = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    t = dt.datetime.fromtimestamp(int(epoch), dt.timezone.utc) if epoch \
        else dt.datetime.now(dt.timezone.utc)
    return t.strftime(model.TIME_FORMAT)


def _github():
    from .net import GitHub
    return GitHub()


def cmd_validate(args) -> int:
    model.load_valid(args.root)
    print("valid")
    return 0


def cmd_build(args) -> int:
    data = model.build(args.root, args.generated or _generated(args.root))
    out = (args.out or args.root / "dist") / "v1" / "index.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    index = json.loads(data)
    print(f"wrote {out}: {len(index['mods'])} mods, "
          f"{sum(len(m['versions']) for m in index['mods'])} versions, "
          f"{len(index['weft'])} Weft releases, {len(index['revoked'])} revoked")
    return 0


def cmd_add_release(args) -> int:
    from .release import add_release
    _, title, body = add_release(_github(), args.root, args.repo, args.tags, args.reviewed_by,
                                 args.reviewed_at, update_meta=args.update_meta, force=args.force)
    pr = args.root / "dist" / "pr.md"
    pr.parent.mkdir(exist_ok=True)
    pr.write_text(body, encoding="utf-8")
    (pr.parent / "pr-title.txt").write_text(title + "\n", encoding="utf-8")
    print(f"\nvalid. Now commit, push and open the pull request:\n\n"
          f"  git add mods weft.toml && git commit -m {shlex.quote(title)}\n"
          f"  git push -u origin HEAD\n"
          f"  gh pr create --title {shlex.quote(title)} --body-file dist/pr.md\n")
    return 0



def cmd_sync_weft(args) -> int:
    from .weft import sync_weft
    added = sync_weft(_github(), args.root)
    print(f"added {', '.join(added)}" if added else "weft.toml is up to date")
    return 0


def cmd_check(args) -> int:
    from .check import changed_version_files, check
    only = changed_version_files(args.root, args.since) if args.since else None
    if only is not None:
        print(f"checking {len(only)} changed version file(s) since {args.since}")
    errors = check(_github(), args.root, only)
    for err in errors:
        print(f"error: {err}", file=sys.stderr)
    return 1 if errors else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m weftindex")
    p.add_argument("--root", type=Path, default=ROOT, help="the index repository")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("add-release", help="write the version file of each GitHub release")
    a.add_argument("repo", help="owner/name of the mod's GitHub repo")
    a.add_argument("tags", nargs="+", metavar="tag", help="<id>-v<version>")
    a.add_argument("--reviewed-by", help="GitHub login of the reviewer (default: yours)")
    a.add_argument("--reviewed-at", help="YYYY-MM-DD (default: today, UTC)")
    a.add_argument("--update-meta", action="store_true",
                   help="also rewrite mod.toml from this release")
    a.add_argument("--force", action="store_true", help="overwrite an existing version file")
    a.set_defaults(func=cmd_add_release)

    sub.add_parser("validate", help="check every file offline").set_defaults(func=cmd_validate)

    b = sub.add_parser("build", help="validate and write <out>/v1/index.json")
    b.add_argument("--out", type=Path, help="default: <root>/dist")
    b.add_argument("--generated", help="RFC 3339 UTC time (default: last commit's time)")
    b.set_defaults(func=cmd_build)

    c = sub.add_parser("check", help="re-download and re-verify what the index pins (online)")
    c.add_argument("--since", metavar="REF",
                   help="only version files changed since the merge base with REF")
    c.set_defaults(func=cmd_check)

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
