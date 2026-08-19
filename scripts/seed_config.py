#!/usr/bin/env python3
"""Seed this repo's config/ tree from a live Home Assistant /config directory.

This is the bootstrap that must happen *before* the first deploy. Until the
repo holds a faithful copy of the live configuration, "the repo is the
source of truth" is not yet true, and running sync_config.py would overwrite
a working instance with whatever skeleton happens to be committed.

Direction is the whole point: this script reads the live instance and writes
into the repo. It is the mirror image of sync_config.py and shares its deny
list, so a secret cannot enter git history by this route.

    python scripts/seed_config.py --source H:/                     # preview
    python scripts/seed_config.py --source H:/ --only packages --apply
    python scripts/seed_config.py --source H:/ --apply

Review the resulting diff before committing. The deny list is a backstop,
not a substitute for reading what you are about to commit.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sync_config import DENY, HA_MARKERS, classify, looks_like_ha_config, matches  # noqa: E402

# Everything sync_config refuses to deploy, plus things that exist on a live
# instance but are not authored config and do not belong in git: HA's own
# runtime scaffolding, and anything a package manager put there.
IMPORT_DENY = DENY + (
    "deps",
    "deps/*",
    ".HA_VERSION",
    ".uuid",
    ".cache",
    ".cache/*",
    "backups",
    "backups/*",
    "image",
    "image/*",
    "www/community",           # HACS-downloaded frontend resources
    "www/community/*",
    "custom_components/hacs",  # HACS itself, installed not authored
    "custom_components/hacs/*",
    "*.db-journal",
    "zwcfg_*.xml",
    "zwscene.xml",
    "OZW_Log.txt",
    ".git",
    ".git/*",
)


def is_import_denied(rel: Path) -> bool:
    """True if this path must never be copied from a live instance into git."""
    return matches(rel, IMPORT_DENY)


def selected(rel: Path, only: list[str]) -> bool:
    """True if --only was not given, or this path is under one of its terms."""
    if not only:
        return True
    posix = rel.as_posix()
    for term in only:
        if posix == term or posix.startswith(term.rstrip("/") + "/"):
            return True
        if matches(rel, (term,)):
            return True
    return False


def plan(source: Path, dest: Path, only: list[str]):
    """Return (actions, skipped) where actions is [(relative_path, verdict)]."""
    actions: list[tuple[Path, str]] = []
    skipped: list[Path] = []
    for src in sorted(p for p in source.rglob("*") if p.is_file()):
        rel = src.relative_to(source)
        if is_import_denied(rel):
            skipped.append(rel)
            continue
        if not selected(rel, only):
            continue
        actions.append((rel, classify(src, dest / rel)))
    return actions, skipped


def dest_is_dirty(dest: Path) -> bool | None:
    """True/False if git can tell us, None if it cannot (not a repo, no git)."""
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "--", str(dest)],
            cwd=dest.parent if dest.parent.exists() else None,
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return bool(out.stdout.strip())


def apply(source: Path, dest: Path, actions) -> int:
    import shutil
    written = 0
    for rel, verdict in actions:
        if verdict == "same":
            continue
        dst = dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel, dst)
        written += 1
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, type=Path,
                    help="live HA /config directory, e.g. a Samba mount")
    ap.add_argument("--dest", default=Path("config"), type=Path,
                    help="repo config tree to seed (default: config)")
    ap.add_argument("--only", action="append", default=[], metavar="PATH",
                    help="limit to this file or subtree, repeatable "
                         "(e.g. --only packages --only automations.yaml)")
    ap.add_argument("--apply", action="store_true",
                    help="actually write; without this the run is a preview")
    ap.add_argument("--force", action="store_true",
                    help="skip the check that --source looks like an HA config dir")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="seed even if --dest has uncommitted changes")
    args = ap.parse_args(argv)

    if not args.source.is_dir():
        print(f"error: source {args.source} is not a directory", file=sys.stderr)
        return 2
    if not args.force and not looks_like_ha_config(args.source):
        print(f"error: {args.source} has no {' or '.join(HA_MARKERS)} — "
              f"this does not look like an HA config directory. "
              f"Pass --force to override.", file=sys.stderr)
        return 2

    if args.apply and not args.allow_dirty:
        dirty = dest_is_dirty(args.dest)
        if dirty:
            print(f"error: {args.dest} has uncommitted changes. Seeding would "
                  f"overwrite them. Commit or stash first, or pass "
                  f"--allow-dirty.", file=sys.stderr)
            return 2

    args.dest.mkdir(parents=True, exist_ok=True)
    actions, skipped = plan(args.source, args.dest, args.only)

    counts = {"new": 0, "changed": 0, "same": 0}
    for rel, verdict in actions:
        counts[verdict] += 1
        if verdict != "same":
            print(f"  {verdict:8s} {rel.as_posix()}")
    for rel in skipped:
        print(f"  {'skipped':8s} {rel.as_posix()} (never enters the repo)")

    print(f"\n{counts['new']} new, {counts['changed']} changed, "
          f"{counts['same']} identical, {len(skipped)} skipped")

    if not args.apply:
        print("dry run — nothing written. Re-run with --apply to seed.")
        return 0

    written = apply(args.source, args.dest, actions)
    print(f"wrote {written} file(s) into {args.dest}")
    print("\nReview before committing — the deny list is a backstop, not a "
          "substitute for reading the diff:")
    print("  git diff --stat && git diff")
    print("  check for tokens, MAC addresses, coordinates, and external URLs")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        # stdout was closed by a downstream reader, e.g. `| head`. Redirect
        # the fd so the interpreter's own flush at exit cannot fail too.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        raise SystemExit(0)
