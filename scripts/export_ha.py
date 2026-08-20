#!/usr/bin/env python3
"""Export Home Assistant's .storage-backed configuration into this repo.

Direction is live -> repo, like seed_config.py. The difference is *how*.

seed_config copies files verbatim, so it must refuse `.storage/` outright:
those files interleave authored configuration with API keys, the auth store,
and machine state. Refusing them is correct, but it leaves the single most
valuable artifact in this project — the conversation agent prompts — with no
representation in git at all. Prompts are config-flow only; they cannot be
expressed as YAML under `config/`, so no amount of restructuring that tree
would ever capture them.

This script reads the same `.storage/` files and emits only the authored,
secret-free subset. Extraction rather than copying is the whole point.

    python scripts/export_ha.py --source H:/            # preview
    python scripts/export_ha.py --source H:/ --apply

**This tree is not a deploy source.** sync_config.py deploys `config/` and
nothing else, and the output lives outside it deliberately so a deploy can
never pick it up. Restoring from these files is a manual operation today.

Two deliberate omissions, both to keep diffs meaningful:

  * `created_at` / `modified_at` timestamps churn on every HA write and say
    nothing about what the config *is*.
  * `input_select` option *values* are excluded. The only ones here are
    runtime buffers (the music recall list rewrites itself on every play),
    so committing them would bury real changes under song titles. The
    definition is kept; the values live in HA's own snapshots.

Review the diff before committing. Redaction is a backstop, not a substitute
for reading what you are about to commit.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sync_config import HA_MARKERS, looks_like_ha_config  # noqa: E402

REDACTED = "**redacted**"

# Keys whose value is never safe to commit, matched case-insensitively on the
# key name alone. Coordinates are included because CLAUDE.md treats them as
# secrets; a *template* that reads them at runtime is fine and is not a value.
SECRET_KEYS = frozenset({
    "api_key",
    "access_token",
    "refresh_token",
    "token",
    "password",
    "client_secret",
    "client_id",
    "secret",
    "private_key",
    "encryption_key",
    "noise_psk",
    "latitude",
    "longitude",
    "elevation",
})

# Values that look like credentials regardless of the key they arrived under.
# The OpenRouter config entry's *title* is literally its API key prefix, which
# is exactly the kind of leak a key-name check alone would miss.
# `sk-` is matched loosely on purpose. HA stores an *elided* key as the config
# entry title (shaped like "sk-or-v1-abc...xyz"), which a length-based pattern
# misses while it still discloses both ends of a live credential. The prefix
# alone is signal enough; nothing else here legitimately starts with it.
SECRET_VALUE_RE = re.compile(
    r"""(
          \bsk-[A-Za-z0-9._\-]{4,}      # OpenAI / OpenRouter keys, full or elided
        | \bBearer\s+[A-Za-z0-9._\-]+   # bearer tokens
        | \beyJ[A-Za-z0-9._\-]{20,}     # JWTs
        | \b[0-9a-f]{40,}\b             # long hex digests and access tokens
    )""",
    re.VERBOSE,
)

# Present on every config entry, and pure churn.
VOLATILE_KEYS = frozenset({"created_at", "modified_at", "discovery_keys"})


def looks_secret(value: object) -> bool:
    """True if this value resembles a credential on its own merits."""
    return isinstance(value, str) and bool(SECRET_VALUE_RE.search(value))


def scrub(obj):
    """Recursively redact secrets and drop volatile keys.

    Redaction is driven by both the key name and the value's own shape, so a
    credential stored under an innocuous key (an entry *title*, say) is still
    caught.
    """
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if key in VOLATILE_KEYS:
                continue
            if isinstance(key, str) and key.lower() in SECRET_KEYS:
                out[key] = REDACTED
            else:
                out[key] = scrub(value)
        return out
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    if looks_secret(obj):
        return REDACTED
    return obj


def load_storage(source: Path, name: str):
    """Return the `data` payload of a .storage file, or None if absent."""
    path = source / ".storage" / name
    if not path.is_file():
        return None
    with path.open(encoding="utf-8") as fh:
        return json.load(fh).get("data")


def export_conversation_agents(source: Path):
    """Config entries that carry an LLM prompt, with their subentries.

    This is the artifact the whole script exists for. Entries without
    subentries are skipped: they hold credentials and nothing authored.
    """
    data = load_storage(source, "core.config_entries")
    if not data:
        return None
    entries = []
    for entry in data.get("entries", []):
        subentries = entry.get("subentries") or []
        if not subentries:
            continue
        if not any((s.get("data") or {}).get("prompt") for s in subentries):
            continue
        entries.append(scrub({
            "domain": entry.get("domain"),
            "title": entry.get("title"),
            "entry_id": entry.get("entry_id"),
            "version": entry.get("version"),
            "minor_version": entry.get("minor_version"),
            "options": entry.get("options") or {},
            "subentries": [
                {
                    "subentry_id": s.get("subentry_id"),
                    "subentry_type": s.get("subentry_type"),
                    "title": s.get("title"),
                    "data": s.get("data") or {},
                }
                for s in subentries
            ],
        }))
    return {"entries": sorted(entries, key=lambda e: (e["domain"], e["entry_id"]))} if entries else None


def export_helpers(source: Path):
    """input_select definitions, without their runtime option values."""
    data = load_storage(source, "input_select")
    if not data:
        return None
    items = []
    for item in data.get("items", []):
        kept = {k: v for k, v in item.items() if k != "options"}
        kept["options_count"] = len(item.get("options") or [])
        items.append(scrub(kept))
    return {"input_select": sorted(items, key=lambda i: i.get("id", ""))} if items else None


def export_pipelines(source: Path):
    """Assist pipelines — which STT, TTS, voice and agent are wired together."""
    data = load_storage(source, "assist_pipeline.pipelines")
    if not data:
        return None
    items = [scrub(i) for i in data.get("items", [])]
    if not items:
        return None
    return {
        "preferred_pipeline": data.get("preferred_item"),
        "pipelines": sorted(items, key=lambda i: i.get("id", "")),
    }


def export_exposed_entities(source: Path):
    """Which entities each voice assistant can see."""
    data = load_storage(source, "homeassistant.exposed_entities")
    if not data:
        return None
    exposed = data.get("exposed_entities") or {}
    return {
        "assistants": data.get("assistants") or {},
        "exposed_entities": {k: scrub(v) for k, v in sorted(exposed.items())},
    } or None


EXPORTS = (
    ("conversation_agents.yaml", export_conversation_agents),
    ("helpers.yaml", export_helpers),
    ("pipelines.yaml", export_pipelines),
    ("exposed_entities.yaml", export_exposed_entities),
)

HEADER = (
    "# Generated by scripts/export_ha.py from Home Assistant's .storage tree.\n"
    "# Backup and review artifact only — this is NOT deployed by sync_config.py,\n"
    "# and editing it does not change the live instance.\n"
    "# Secrets are redacted; re-read the diff before committing anyway.\n"
)


def render(source: Path) -> dict[str, str]:
    """Return {filename: yaml text} for everything that could be exported."""
    out: dict[str, str] = {}
    for filename, fn in EXPORTS:
        payload = fn(source)
        if payload is None:
            continue
        body = yaml.dump(
            payload,
            Dumper=_BlockDumper,
            sort_keys=True,
            default_flow_style=False,
            allow_unicode=True,
            width=100,
        )
        out[filename] = HEADER + body
    return out


class _BlockDumper(yaml.SafeDumper):
    """Dumps multi-line strings as literal blocks instead of quoted scrawl.

    Prompts are the reason this script exists and are the thing a human will
    actually read here. Default quoting reflows them onto wrapped lines and
    doubles every apostrophe, so a one-word edit shows up as a rewritten
    paragraph. Literal style keeps the text verbatim and the diff honest.
    """


def _represent_str(dumper: yaml.Dumper, data: str):
    # Literal style cannot round-trip a line with trailing whitespace, so fall
    # back to the default representation rather than corrupting the value.
    if "\n" in data and not any(line != line.rstrip() for line in data.split("\n")):
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_BlockDumper.add_representer(str, _represent_str)


def classify(text: str, dest: Path) -> str:
    if not dest.exists():
        return "new"
    return "same" if dest.read_text(encoding="utf-8") == text else "changed"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, type=Path,
                    help="live HA /config directory, e.g. a Samba mount")
    ap.add_argument("--dest", default=Path("ha_export"), type=Path,
                    help="repo tree to write into (default: ha_export)")
    ap.add_argument("--apply", action="store_true",
                    help="actually write; without this the run is a preview")
    ap.add_argument("--force", action="store_true",
                    help="skip the check that --source looks like an HA config dir")
    args = ap.parse_args(argv)

    if not args.source.is_dir():
        print(f"error: source {args.source} is not a directory", file=sys.stderr)
        return 2
    if not args.force and not looks_like_ha_config(args.source):
        print(f"error: {args.source} has no {' or '.join(HA_MARKERS)} — "
              f"this does not look like an HA config directory. "
              f"Pass --force to override.", file=sys.stderr)
        return 2

    rendered = render(args.source)
    if not rendered:
        print("nothing to export — no readable .storage payloads found",
              file=sys.stderr)
        return 1

    counts = {"new": 0, "changed": 0, "same": 0}
    for filename, text in sorted(rendered.items()):
        verdict = classify(text, args.dest / filename)
        counts[verdict] += 1
        print(f"  {verdict:8s} {filename}")

    print(f"\n{counts['new']} new, {counts['changed']} changed, "
          f"{counts['same']} identical")

    if not args.apply:
        print("dry run — nothing written. Re-run with --apply to export.")
        return 0

    args.dest.mkdir(parents=True, exist_ok=True)
    for filename, text in sorted(rendered.items()):
        (args.dest / filename).write_text(text, encoding="utf-8")
    print(f"wrote {len(rendered)} file(s) into {args.dest}")
    print("\nReview before committing — redaction is a backstop, not a "
          "substitute for reading the diff:")
    print("  git diff --stat && git diff")
    print("  check for tokens, coordinates, and external URLs")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        raise SystemExit(0)
