"""python3 -m weftindex <command>; see README.md, "Tooling"."""

from __future__ import annotations

import argparse
import datetime as dt
import os
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
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _github():
    from .net import GitHub
    return GitHub()


def cmd_build(args) -> int:
    generated = args.generated or _generated(args.root)
    try:
        data = model.build(args.root, generated)
    except model.ValidationError as e:
        for err in e.errors:
            print(f"error: {err}", file=sys.stderr)
        print(f"{len(e.errors)} error(s)", file=sys.stderr)
        return 1
    if args.validate_only:
        print("valid")
        return 0
    out = args.out / "v1" / "index.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    repo = model.load_valid(args.root)
    n_versions = sum(len(vs) for _, vs in repo.mods.values())
    print(f"wrote {out}: {len(repo.mods)} mods, {n_versions} versions, "
          f"{len(repo.weft)} Weft releases, {len(repo.revoked)} revoked")
    return 0


def cmd_add_release(args) -> int:
    from .release import ReleaseError, add_release
    gh = _github()
    reviewer = args.reviewed_by or gh.api("/user")["login"]
    try:
        for tag in args.tags:
            add_release(gh, args.root, args.repo, tag, reviewer, args.reviewed_at,
                        update_meta=args.update_meta, force=args.force)
    except ReleaseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_sync_weft(args) -> int:
    from .release import sync_weft
    added = sync_weft(_github(), args.root)
    print(f"added {', '.join(added)}" if added else "weft.toml is up to date")
    return 0


def cmd_check(args) -> int:
    from .check import changed_version_files, check
    only = changed_version_files(args.root, args.since) if args.since else None
    if only is not None:
        print(f"checking {len(only)} changed version file(s) since {args.since}")
    try:
        errors = check(_github(), args.root, only)
    except model.ValidationError as e:
        errors = e.errors
    for err in errors:
        print(f"error: {err}", file=sys.stderr)
    return 1 if errors else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m weftindex")
    p.add_argument("--root", type=Path, default=ROOT, help="the index repository")
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="validate and write dist/v1/index.json")
    b.add_argument("--out", type=Path, default=ROOT / "dist")
    b.add_argument("--generated", help="RFC 3339 UTC time (default: last commit's time)")
    b.add_argument("--validate-only", action="store_true", help="validate, write nothing")
    b.set_defaults(func=cmd_build)

    v = sub.add_parser("validate", help="same as build --validate-only")
    v.set_defaults(func=cmd_build, validate_only=True, generated="1970-01-01T00:00:00Z", out=None)

    a = sub.add_parser("add-release", help="write the version file of a GitHub release")
    a.add_argument("repo", help="owner/name of the mod's GitHub repo")
    a.add_argument("tags", nargs="+", metavar="tag", help="<id>-v<version>")
    a.add_argument("--reviewed-by", help="GitHub login of the reviewer (default: gh user)")
    a.add_argument("--reviewed-at", help="YYYY-MM-DD (default: today, UTC)")
    a.add_argument("--update-meta", action="store_true",
                   help="also rewrite mod.toml from this release's source")
    a.add_argument("--force", action="store_true", help="overwrite an existing version file")
    a.set_defaults(func=cmd_add_release)

    s = sub.add_parser("sync-weft", help="add new Weft releases and their ABI to weft.toml")
    s.set_defaults(func=cmd_sync_weft)

    c = sub.add_parser("check", help="re-download and re-verify what the index pins")
    c.add_argument("--since", metavar="REF",
                   help="only version files changed since the merge base with REF")
    c.set_defaults(func=cmd_check)

    args = p.parse_args(argv)
    args.root = args.root.resolve()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
