#!/usr/bin/env python3
"""Copy this repo's config/ tree onto a live Home Assistant /config directory.

Deploying config changes what a physical house does, so this script is
deliberately conservative:

  * dry-run by default — nothing is written without --apply
  * never copies secrets or runtime state, even if such a file is staged
  * never deletes anything in the target
  * refuses a target that does not look like an HA config directory
  * optionally snapshots every file it is about to overwrite

Typical use, with HA's /config mounted over Samba as drive H:

    python scripts/sync_config.py --target H:/                 # preview
    python scripts/sync_config.py --target H:/ --apply --backup .backups/
"""

from __future__ import annotations

import argparse
import filecmp
import fnmatch
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Files that must never leave this repo for a live instance, and files that
# belong to HA's runtime rather than to source control. Matched against the
# path relative to the source root, with fnmatch semantics per component.
DENY = (
    "secrets.yaml",
    ".env",
    "*.pem",
    "*.key",
    ".storage",
    ".storage/*",
    ".cloud",
    ".cloud/*",
    "home-assistant_v2.db*",
    "*.log",
    "*.log.*",
    "known_devices.yaml",
    "ip_bans.yaml",
    "tts",
    "tts/*",
    "__pycache__",
    "__pycache__/*",
    "*.pyc",
    ".DS_Store",
)

# A directory containing one of these is plausibly an HA config directory.
HA_MARKERS = ("configuration.yaml", ".HA_VERSION")


def is_denied(rel: Path) -> bool:
    """True if any component or the whole relative path matches a deny rule."""
    posix = rel.as_posix()
    for pattern in DENY:
        if fnmatch.fnmatch(posix, pattern):
            return True
        if any(fnmatch.fnmatch(part, pattern) for part in rel.parts):
            return True
    return False


def looks_like_ha_config(target: Path) -> bool:
    return any((target / marker).exists() for marker in HA_MARKERS)


def classify(src: Path, dst: Path) -> str:
    if not dst.exists():
        return "new"
    # shallow=False: compare contents, not just size and mtime. Samba mtimes
    # are not trustworthy enough to skip a real comparison.
    return "same" if filecmp.cmp(src, dst, shallow=False) else "changed"


def plan(source: Path, target: Path) -> tuple[list[tuple[Path, str]], list[Path]]:
    """Return (actions, skipped) where actions is [(relative_path, verdict)]."""
    actions: list[tuple[Path, str]] = []
    skipped: list[Path] = []
    for src in sorted(p for p in source.rglob("*") if p.is_file()):
        rel = src.relative_to(source)
        if is_denied(rel):
            skipped.append(rel)
            continue
        actions.append((rel, classify(src, target / rel)))
    return actions, skipped


def apply(source: Path, target: Path, actions, backup: Path | None) -> int:
    written = 0
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for rel, verdict in actions:
        if verdict == "same":
            continue
        src, dst = source / rel, target / rel
        if verdict == "changed" and backup is not None:
            snap = backup / stamp / rel
            snap.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, snap)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        written += 1
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="config", type=Path,
                    help="repo config tree to deploy (default: config)")
    ap.add_argument("--target", required=True, type=Path,
                    help="live HA /config directory, e.g. a Samba mount")
    ap.add_argument("--apply", action="store_true",
                    help="actually write; without this the run is a preview")
    ap.add_argument("--backup", type=Path, default=None,
                    help="snapshot every overwritten file under this directory")
    ap.add_argument("--force", action="store_true",
                    help="skip the check that --target looks like an HA config dir")
    args = ap.parse_args(argv)

    if not args.source.is_dir():
        print(f"error: source {args.source} is not a directory", file=sys.stderr)
        return 2
    if not args.target.is_dir():
        print(f"error: target {args.target} is not a directory", file=sys.stderr)
        return 2
    if not args.force and not looks_like_ha_config(args.target):
        print(f"error: {args.target} has no {' or '.join(HA_MARKERS)} — "
              f"refusing to write to what may be the wrong directory. "
              f"Pass --force to override.", file=sys.stderr)
        return 2

    actions, skipped = plan(args.source, args.target)
    counts = {"new": 0, "changed": 0, "same": 0}
    for rel, verdict in actions:
        counts[verdict] += 1
        if verdict != "same":
            print(f"  {verdict:8s} {rel.as_posix()}")
    for rel in skipped:
        print(f"  {'skipped':8s} {rel.as_posix()} (never deployed)")

    print(f"\n{counts['new']} new, {counts['changed']} changed, "
          f"{counts['same']} identical, {len(skipped)} skipped")

    if not args.apply:
        print("dry run — nothing written. Re-run with --apply to deploy.")
        return 0

    written = apply(args.source, args.target, actions, args.backup)
    print(f"wrote {written} file(s) to {args.target}")
    if args.backup:
        print(f"overwritten originals snapshotted under {args.backup}")
    print("\nReload or restart HA for changes to take effect. "
          "Validate first: hass --script check_config -c <target>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
