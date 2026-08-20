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

## Two channels, deliberately separate

Nothing here speaks a Home Assistant protocol of its own. Work reaches the
instance over two independent channels, and keeping them separate is a
design decision, not an accident of implementation:

| Channel | Carries | Credential | Cannot |
| --- | --- | --- | --- |
| SMB / Samba | files onto `/config` | share username + password, held by the OS mount | reload, restart, or read state |
| ha-mcp | registry reads, template evaluation, service calls, traces | the add-on's secret URL path | write files into `/config` |

`sync_config.py` uses neither an HA API nor a token — it copies between two
directory paths and is protocol-agnostic. Pointed at a local directory it
behaves identically, which is how its tests run.

**The deploy path holds no Home Assistant credential.** That is the
strongest safety property of this setup: there is no long-lived access token
to leak into the repo, a chat log, or CI, and if the mount is absent the
script fails at "target is not a directory" rather than finding another
route in. **Preserve it.** Adding a convenience `--reload` flag that calls
HA's REST API would drag a token into the deploy path and give the script
network reach it currently cannot have — do not add one without deciding
that trade deliberately.

**Consequence: a deploy is inert until something else activates it.** A file
copy cannot make HA re-read config. The script stops after writing and says
so. Activation is the other channel's job — `homeassistant.reload_all` or a
targeted reload via ha-mcp, or a restart from the UI — and verification is
too. The script reporting "wrote 1 file" is evidence about the filesystem
and nothing more; whether HA parsed and applied it is a separate question
only the API channel can answer.

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

## Bootstrapping: seed the repo before the first deploy

**Do this before running `sync_config.py` for the first time.** Until the
repo holds a faithful copy of the live configuration, "the repo is the
source of truth" is a claim, not a fact — and deploying a skeleton
`configuration.yaml` over a working instance looks exactly like a correct
deploy. The deploy guards do not catch a wrong-direction sync.

`scripts/seed_config.py` runs the other way: it reads the live instance and
writes into `config/`. It shares the deploy deny list, so secrets and
runtime state cannot enter git by this route.

Work in small, reviewable steps rather than one bulk import:

```
python scripts/seed_config.py --source H:/                      # preview everything
python scripts/seed_config.py --source H:/ --only packages --apply
git diff                                                        # read it
git add config/ && git commit
python scripts/seed_config.py --source H:/ --only automations.yaml --apply
```

**Review every diff before committing.** The deny list is a backstop, not a
substitute for reading what you are about to publish — a hand-written
comment or a hardcoded URL in an automation will pass every automated check
and still not belong in git.

What it refuses to import, on top of the deploy deny list: `deps/`,
`.HA_VERSION`, `.uuid`, `.ha_run.lock`, `backups/`, `image/`, `.cache/`,
HACS-downloaded resources under `www/community/`, the HACS component itself,
Z-Wave dumps, and any nested `.git/`.

**It will not seed over uncommitted changes.** Untracked files in `config/`
count as uncommitted, so a second scoped seed is blocked until the first is
committed. That is deliberate — untracked work is the one thing git cannot
recover. `--allow-dirty` overrides it.

**Verifying the seed worked:** deploy straight back with no `--apply`. A
faithful seed reports `0 new, 0 changed` — the repo and the instance agree.
Anything else means something was missed or edited in between.

## What seeding cannot reach: `.storage/`

Seeding covers everything that *is* a file. It does not cover `.storage/`,
and it never should: those files interleave authored configuration with the
API keys, the auth store, and the entity registry. Copying them wholesale
would put credentials in git.

But that is also where the conversation agent prompts live — the artifact
this project has iterated on hardest — along with helper definitions, Assist
pipelines, and exposure settings. Prompts are config-flow only; there is no
YAML form of them to seed. Left alone, the single most valuable thing in the
system has no representation in git.

`scripts/export_ha.py` closes that gap by *extracting* rather than copying:

```
python scripts/export_ha.py --source H:/            # preview
python scripts/export_ha.py --source H:/ --apply    # writes ha_export/
git diff ha_export/                                 # read it
```

It redacts by key name *and* by value shape, drops churny timestamps, and
writes multi-line prompts as literal blocks so an edit diffs as an edit
rather than a reflowed paragraph.

**`ha_export/` is a backup and review artifact, not a deploy source.**
`sync_config.py` only ever reads `config/`; nothing here is ever pushed to
the instance, and editing these files changes nothing. Restoring from them
is manual today.

**Re-run it whenever a prompt, script, helper, or pipeline changes**, and
commit the result with the change — that commit is the entire review trail
for config that otherwise goes straight to production.

### The trap that already bit

The OpenRouter config entry's **title is its API key**, stored elided —
shaped like `sk-or-v1-abc...xyz`. Redaction driven by key name alone keeps
it, and the first real export did exactly that: the value pattern expected a
full-length key, and the elided form is short and full of dots. Grepping the
output before committing caught it; the tooling did not.

It then nearly happened a second way. The fix was written up using the
instance's *real* elided key as the illustrative example — in this document,
a code comment, and a test fixture — which would have committed the very
value that had just been redacted out of the export. Also caught by the same
grep.

Two rules this earns:

- **Grep the export for credential shapes before every commit.** The tests
  encode the known cases, but only cover shapes someone already thought of.
- **Never paste a real credential into a doc, comment, or fixture, even an
  elided one.** Use an obviously fake value of the same shape.

## The loop

0. **Seed first, once**, if `config/` does not yet mirror the instance —
   see above.
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
- **Whether `ha_export/` is stale** — `export_ha.py --source … ` with no
  `--apply`; `0 changed` means the committed copy matches the instance
- **Whether the live config is valid** — `hass --script check_config -c /config`
- **What HA actually loaded** — the HA log after a reload, plus a state read
  on an entity the change should have affected
