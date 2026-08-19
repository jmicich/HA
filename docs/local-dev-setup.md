# Local dev setup — Claude Code against a live /config

Status as of 2026-08-19. Covers running Claude Code on the Windows host with
Home Assistant's `/config` mounted over Samba, and deploying this repo's
config tree onto the live instance.

Read `dev-environment.md` first — it covers the ha-mcp ↔ Claude loop, which
this complements rather than replaces.

## Why this topology

Two constraints decide it, both verified rather than assumed:

- **ha-mcp cannot write `/config`.** The file-tools component is an optional
  HACS beta and is not installed. Everything this repo exists to produce —
  packages, custom sentences, `intent_script`, trigger-based template
  entities — is YAML that must land in `/config`. MCP can read the system
  and call services; it cannot put files there.
- **A cloud Claude Code session cannot reach the instance at all.** Sessions
  on claude.ai run behind a policy-enforcing egress proxy: LAN ranges are
  unroutable from the container, and public HA endpoints are refused by
  egress policy (403). This is independent of how HA is exposed — remote
  access being enabled does not change it. Verified 2026-08-19.

So file-level work happens locally, on the machine that can see both the git
repo and the Samba share. Use each tool for what it is good at:

| Task | Tool |
| --- | --- |
| Writing and reviewing YAML, git history, tests | Claude Code, locally |
| Reading registries, evaluating templates, execution traces | ha-mcp via Claude Desktop |
| Sending utterances closed-loop (`conversation.process`) | ha-mcp via Claude Desktop |

## One-time setup

**1. Expose `/config` over Samba.** Install the Samba share add-on in HA,
set a username and password, and enable *Start on boot*. Restrict it to the
LAN; do not expose it through the remote-access tunnel.

**2. Map it on Windows.** Map the `config` share to a drive letter (`H:` in
the examples below). Map it persistently so the path survives a reboot.
Confirm `H:\configuration.yaml` is readable before going further.

**3. Clone this repo** somewhere that is *not* the share — the repo and the
live config are deliberately separate trees:

```
git clone https://github.com/jmicich/HA.git
cd HA
```

**4. Install Claude Code** and run it from the repo root.

**5. Install the dev dependencies** for the test suite:

```
pip install -r requirements-dev.txt
```

## The loop

1. **Edit in the repo**, never on the share. The share is a deploy target,
   not a workspace — edits made there are invisible to git and are silently
   overwritten by the next deploy.
2. **Validate locally.** `pytest tests/ -q` for tooling, `yamllint config/`
   for YAML. Both run in CI on every push.
3. **Preview the deploy.** Dry run is the default:
   ```
   python scripts/sync_config.py --target H:/
   ```
   Read the plan. `new` and `changed` are what will be written; `skipped`
   are files the script refuses to deploy at all.
4. **Deploy, with a snapshot of anything overwritten:**
   ```
   python scripts/sync_config.py --target H:/ --apply --backup .backups/
   ```
5. **Check the config before restarting.** In an HA terminal, or via the
   Terminal add-on: `hass --script check_config -c /config`. A restart on a
   bad config leaves the instance down.
6. **Reload or restart HA**, then verify behaviour against system state —
   not against what anything reports it did.
7. **Commit and push**, then open a PR.

## What sync_config.py will not do

The script deploys into a live house, so it is deliberately conservative:

- **Dry run by default.** Nothing is written without `--apply`.
- **Never deploys secrets or runtime state** — `secrets.yaml`, `.env`, keys,
  `.storage/`, the recorder database, logs, `known_devices.yaml`,
  `ip_bans.yaml`, `tts/`. These are refused even if staged by mistake, and a
  `secrets.yaml` already on the live instance is left untouched.
- **Never deletes.** Files on the instance that are absent from the repo are
  left alone. Removing something from the live config is a manual act.
- **Refuses an unrecognised target.** A directory with no `configuration.yaml`
  or `.HA_VERSION` is rejected, so a mistyped drive letter cannot scatter
  YAML across an unrelated folder. `--force` overrides, deliberately noisy.
- **Compares by content, not timestamp.** Samba mtimes are not trustworthy
  enough to skip a real comparison.

`secrets.yaml` lives only on the HA instance. It is never in this repo and
never deployed from it — the values it holds are referenced by `!secret`.

## Safety

Deploying config changes what a physical house does. Before a deploy that
touches locks, alarms, garage doors, or heating, take an HA backup and
confirm the intent — see the confirmation gates in `CLAUDE.md`.

## Traps

**Do not point `--target` at anything but the config share.** The marker
check catches the common mistake, but `--force` disables it.

**The share is not a workspace.** Editing on `H:` and then deploying from
the repo silently reverts your edit — the repo is the source of truth.

**UI-managed automations round-trip through the HA editor.** Deploying a
hand-edited `automations.yaml` over entries the UI owns loses UI edits made
since. Hand-written automation belongs in `packages/`.

**pytest may be installed outside the system Python.** If `python -m pytest`
reports no module, call the `pytest` executable directly — it carries its
own interpreter.

## How to audit this

- **Is the share mounted and writable** — read and write a scratch file on
  the mapped drive
- **What the deploy would change** — `sync_config.py --target … ` with no
  `--apply`
- **Whether the live config is valid** — `hass --script check_config -c /config`
- **What HA actually loaded** — the HA log after a reload, plus a state read
  on an entity the change should have affected
