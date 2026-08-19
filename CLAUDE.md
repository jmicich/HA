# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this repo is

Personal Home Assistant repository: configuration, custom integrations,
external automation code, and documentation for one HA installation.

**Populated so far: `docs/`, `scripts/`, `tests/`, and CI.** The `config/`
and integration trees below are intended layout, not existing state. As real code lands, update this file to
match what actually exists — do not leave stale guidance here.

Start with `docs/project-overview.md` — it is the orientation doc for the
voice-assistant build-out and points to the rest. Those docs follow a
deliberate rule: **they record decisions, traps, and rationale, never
auditable state** (no entity IDs, IPs, or inventories). Each carries a "How
to audit this" section listing the calls that regenerate that information.
Preserve that rule when editing them.

`docs/local-dev-setup.md` covers the working setup: Claude Code on the
Windows host against HA's `/config` over Samba, and how config reaches the
live instance.

## How we work together

These are standing working preferences, not repo trivia. They outrank
convenience: if following them costs an extra step, take the extra step.

### On the loop, not in the loop

**Front-load alignment, then run autonomously.**

- Ask questions at the *start*, batched, before work begins — not drip-fed
  mid-task. Ambiguity that changes the shape of the work is worth one round
  of questions; ambiguity a careful colleague would just resolve is not.
- Once the plan is agreed, execute it without check-ins. Do not ask
  permission for steps already inside the agreed scope.
- Come back mid-task only when: genuinely blocked, the scope turns out
  materially different from what was agreed, the plan is discovered to be
  wrong, or a change hits one of the confirmation gates below.

**Report like briefing a manager.** Lead with the outcome and what it means,
not a narration of steps. State what changed, what it cost, what is still
open, and what needs a decision. Surface risks and things deliberately not
done. Assume the reader was not watching and does not want a transcript.

### Operate like a capable senior employee

- Own the whole task through to done. Finish the unglamorous parts.
- **Show grit.** A first approach failing is not a result. Try the next one.
  Report what was tried and why it failed only if it stays failed.
- When genuinely blocked, return with the blocker *and a proposed way
  forward* — a recommendation, not an open question dumped back.
- If part of the scope is blocked, deliver everything else in full and say
  plainly what was left out and why. Scaling the work down is the user's
  call, not mine.
- Disagree when the premise looks wrong. Say it once, plainly, then follow
  the decision.

### Don't cascade unvalidated assumptions

**Check, don't guess, when it's structural.** A structural assumption is one
that later work builds on — a file's location, whether a tool can write
somewhere, what a system currently holds, whether a capability exists.
Guessing wrong there does not produce one error, it produces a chain of them
resting on a false premise.

- Verify structural facts before building on them. One cheap check beats an
  hour of rework.
- Where proceeding on an assumption is unavoidable, state it explicitly as
  an assumption rather than presenting it as fact.
- When a check contradicts something claimed earlier, say so plainly and
  move on — no ceremony.
- This is the same rule the docs in `docs/` call **probe, don't infer**, and
  it applies to the tooling as much as to the system being worked on.

### Verification standards

**Build it and run the tests before calling it done.**

- Reproduce a failure before fixing it, then show the same case passing.
- Verify against real system state, never against something's own report of
  what it did.
- Run the project's own checks — see the validation commands in the YAML and
  custom-component sections below.
- **If validation is impossible in the current environment, say so
  explicitly.** Never describe unvalidated work as verified. "I could not
  run X here" is an acceptable report; silence implying it passed is not.
- Distinguish *verified* from *inferred* when reporting. Both are useful;
  conflating them is not.

**Run the tests for the subsystem you touched**, not just the ones that are
cheap to run. Current mapping:

| Touching | Run |
| --- | --- |
| `script.play_music`, the agent prompt, MA provider ranking or resolution order | the regression suite in `docs/voice-and-music.md` |
| the music recall list, its automation, or the event payload | the same suite — its last two cases exist for recall |

That regression table **is** the music playback integration suite; treat it
as a test suite, not documentation. Extend this mapping as other subsystems
gain suites.

The music suite has constraints that make a careless run worse than no run:

- **Each case at least three times.** Several failures are probabilistic; a
  single green run proves nothing.
- **Space playback calls ~30s apart.** Back-to-back calls trip the provider
  rate limiter, after which every later case fails for unrelated reasons.
- **Audit speaker inventory and room assignments first.** Three cases depend
  on which rooms have speakers, and a room that is empty by accident rather
  than by design makes the empty-room result meaningless.
- **Any prompt change invalidates the whole suite**, not just the cases that
  look prompt-related.
- **Verify by reading player state, never the spoken reply.** The model
  reports actions it did not take.

These run against a live HA instance via `conversation.process`, so they are
not automatable in CI today. Until they are, "run the tests" for music means
running that suite by hand and reporting per-case results — including which
cases were not run.

### Default workflow

Unless told otherwise, every non-trivial task runs:

1. **Plan and align** — state the approach, surface the questions worth
   asking, get agreement.
2. **Implement and validate** — make the change, run the tests, iterate
   until they pass.
3. **Open a PR** for code and doc changes, on a feature branch.
4. **Summarize** — the manager-briefing report described above.

Step 3 is a standing instruction: PRs for code and doc changes are the
default here, not something to ask about each time.

### Confirmation gates

Autonomy has a short exception list. Stop and confirm regardless of prior
alignment when a change would:

- affect physical-house behavior with safety consequences — the rule and its
  scope live in *Working conventions* below, which is canonical;
- be hard to reverse, or destroy state that isn't recoverable;
- publish outside this repo, or widen who can see something.

## Intended layout

```
config/              # Home Assistant /config contents (YAML)
  configuration.yaml
  automations.yaml
  scripts.yaml
  scenes.yaml
  packages/          # feature-scoped bundles (preferred over flat files)
  dashboards/        # Lovelace YAML-mode dashboards
  blueprints/
custom_components/   # Python integrations, one dir per domain
appdaemon/           # AppDaemon apps + apps.yaml
nodered/             # exported flow JSON
pyscript/            # pyscript .py automations
docs/                # setup notes, hardware inventory, network/topology
```

## Never commit secrets

This is the highest-priority rule in this repo.

- All credentials, tokens, API keys, latitude/longitude, and external URLs
  go in `config/secrets.yaml` and are referenced as `!secret <key>`.
  `secrets.yaml` itself is **never** committed.
- Also never commit: `.storage/`, `home-assistant_v2.db*`, `*.log`,
  `known_devices.yaml`, `ip_bans.yaml`, `tts/`, `.cloud/`, backups.
- `.gitignore` covers the above, but treat it as a backstop, not a
  guarantee: a file already tracked stays tracked even after a matching
  ignore rule is added. Before any commit, check the diff for tokens, MAC
  addresses, and coordinates. If a secret was already committed, say so
  plainly — the credential must be rotated, not just removed in a
  follow-up commit.

## Home Assistant YAML

- Prefer `packages/` — one YAML file per feature (e.g. `packages/lighting.yaml`)
  bundling its automations, scripts, helpers, and template entities together.
  Only touch the flat `automations.yaml` for entries the UI editor owns.
- UI-managed automations round-trip through the HA editor; hand-edits there
  can be overwritten. Anything hand-written belongs in a package.
- Entity/`unique_id` naming: `<area>_<device>_<function>`, snake_case,
  e.g. `kitchen_ceiling_light`. Always set `unique_id` on template entities
  and automations so they stay editable and renameable in the UI.
- Use the modern schema: `trigger:`/`condition:`/`action:` with `triggers:`
  style keys as the HA version in use requires; prefer `state` triggers over
  polling templates; use `mode:` explicitly (`single`, `restart`, `queued`).
- Prefer Jinja templates that fail closed — guard with `is_state()` and
  `has_value()` rather than raw attribute access that errors on `unknown`.
- Validate before committing:
  ```
  yamllint config/                          # config in .yamllint, runs in CI
  hass --script check_config -c config/     # on the HA instance
  ```
  `check_config` needs a real HA install, so it runs on the instance rather
  than in CI. If a check cannot be run in the current environment, say so
  rather than claiming the config was validated.

## Custom components (Python)

- One integration per `custom_components/<domain>/`, with `manifest.json`,
  `__init__.py`, `config_flow.py`, `const.py`, and platform modules
  (`sensor.py`, `switch.py`, …).
- Config flow only — no YAML setup for new integrations. `manifest.json`
  needs `domain`, `name`, `version`, `documentation`, `codeowners`,
  `iot_class`, `config_flow: true`, and pinned `requirements`.
- Everything async. Never block the event loop: no `requests`, no
  `time.sleep`, no sync file I/O in the loop — use `aiohttp` via
  `async_get_clientsession(hass)` and `hass.async_add_executor_job` for
  unavoidable sync calls.
- Poll through a `DataUpdateCoordinator` shared by all entities of an
  integration rather than per-entity fetches.
- Entities set `_attr_unique_id` and `_attr_device_info` so they group
  under one device and survive renames.
- Tests live in `tests/components/<domain>/`, use `pytest` with
  `pytest-homeassistant-custom-component`, and mock all network I/O:
  ```
  pytest tests/ -q
  ```

## AppDaemon / Node-RED / pyscript

- AppDaemon: one class per app under `appdaemon/apps/`, registered in
  `apps.yaml`; keep entity IDs in app args, not hardcoded in the class.
- Node-RED: commit exported flow JSON. Diffs are noisy — describe the
  behavior change in the commit message, since the JSON won't show it.
- pyscript: keep functions small and decorator-driven
  (`@state_trigger`, `@service`).

## Docs

Markdown in `docs/`. Prefer documenting *why* a piece of automation exists
and which physical device it depends on — that context is the part that
can't be recovered from the YAML later.

## Deploying to the live instance

The repo is the source of truth; HA's `/config` is a deploy target. Files
are never edited directly on the share — edits there are invisible to git
and are overwritten by the next deploy.

```
python scripts/seed_config.py --source H:/         # bootstrap: live -> repo
pytest tests/ -q                                   # tooling tests
yamllint config/                                   # YAML lint
python scripts/sync_config.py --target H:/         # preview (dry run)
python scripts/sync_config.py --target H:/ --apply --backup .backups/
hass --script check_config -c /config              # on the instance, before restart
```

`sync_config.py` copies files over a Samba mount and holds no HA credential
— it cannot reload, restart, or read state. **A deploy is inert until HA
re-reads config**, which is ha-mcp's job or a restart from the UI, and
verification is a state read, never the script's own output. It is dry-run
by default, never deploys secrets or runtime state, never deletes from the
target, and refuses a directory that does not look like an HA config tree. Its safety properties are covered by
`tests/test_sync_config.py` — extend those tests when changing it.

**Direction matters.** `seed_config.py` goes live → repo and must run before
the first deploy; `sync_config.py` goes repo → live. A wrong-direction sync
overwrites a working instance and looks identical to a correct one, so the
repo must mirror the instance before it can act as its source of truth.

Full procedure and traps: `docs/local-dev-setup.md`.

## Working conventions

- Development happens on feature branches; do not commit directly to `main`.
- Commits are scoped to one feature or fix, with a message stating the
  behavior change ("stop porch light retriggering on motion clear"), not
  the file touched.
- Changing an automation changes what a physical house does. When a change
  affects locks, alarms, garage doors, heating, or anything that could
  strand someone, confirm the intent before committing rather than
  inferring it.
