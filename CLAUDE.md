# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this repo is

Personal Home Assistant repository: configuration, custom integrations,
external automation code, and documentation for one HA installation.

**The repo is currently empty apart from `README.md`.** Sections below
describe the intended layout and conventions. As real code lands, update
this file to match what actually exists — do not leave stale guidance here.

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
  yamllint config/
  hass --script check_config -c config/     # if HA is installed locally
  ```
  If neither tool is available in the environment, say so rather than
  claiming the config was validated.

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

## Working conventions

- Development happens on feature branches; do not commit directly to `main`.
- Commits are scoped to one feature or fix, with a message stating the
  behavior change ("stop porch light retriggering on motion clear"), not
  the file touched.
- Changing an automation changes what a physical house does. When a change
  affects locks, alarms, garage doors, heating, or anything that could
  strand someone, confirm the intent before committing rather than
  inferring it.
