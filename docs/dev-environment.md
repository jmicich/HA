# Dev environment — ha-mcp ↔ Claude

Status as of 2026-08-18. Covers the closed-loop setup that lets Claude read
and write Home Assistant state directly, and how to debug it when it stops
working.

**This doc deliberately records no auditable state** — no IPs, ports, slugs,
versions, or entity IDs beyond what is needed to *find* them. Anything a
tool call can tell you is not written down here; see "How to audit this".
What is recorded is the reasoning, the traps, and the negative results, none
of which an audit can reproduce.

## Why this exists

Iterating by pasting screenshots and logs into chat is slow and error-prone.
With ha-mcp connected, Claude reads the registries, evaluates templates,
calls services, and runs `conversation.process` closed-loop — the
probe-don't-infer workflow, without a human relaying state by hand.

**Concrete measure of the difference:** the "play music in the living room"
failure took roughly an hour of screenshot round-trips and produced two
wrong diagnoses. With MCP access it took four tool calls and a controlled
experiment.

## Which MCP server, and why

Three things in the HA ecosystem share the name. They are not
interchangeable:

| Thing | Direction | Sees |
| --- | --- | --- |
| MCP Server integration (core) | HA as server | Assist API only — exposed entities, intents |
| MCP integration (core) | HA as client | consumes other servers |
| **ha-mcp** (community) | HA as server | REST + WebSocket — registries, config, automations, dashboards |

**Only ha-mcp is useful for this project.** The official server routes
through Assist, so it cannot read the entity registry, integration
ownership, supported features, or exposure settings — exactly the data
needed to debug voice targeting.

## Setup

Add the repository `https://github.com/homeassistant-ai/ha-mcp` under
Settings → Apps → App Store → ⋮ → Repositories, then install the add-on with
*Start on boot* and *Watchdog* enabled.

**Do not use the "My Home Assistant" shortcut links** from the docs or setup
wizard. Known HA bug: they open the App Store without the add-repository
dialog, and one chains to a HACS integration page that has never existed.
Add the repository manually.

**HACS is not required** — only for the optional beta YAML/file tools
component. Consequence: `/config` is not writable from MCP. Anything
YAML-only (custom sentences, `intent_script:`, trigger-based template
entities) needs File editor, SSH, or Samba.

The add-on prints its URL in its own Log tab on first start. The secret path
in that URL **is the credential** — there is no username or password. Rotate
it by deleting the persisted secret file and restarting the add-on.

Client config lives in Claude Desktop's `claude_desktop_config.json`
(Settings → Developer → Edit Config). Three things there are load-bearing:

- **`fastmcp-remote`, not `mcp-proxy`.** The latter breaks whenever the MCP
  SDK ships a major release.
- **No `HOMEASSISTANT_URL` or `HOMEASSISTANT_TOKEN`.** Those belong to the
  local/stdio install variant. Pasting them alongside the add-on URL is a
  documented cause of connection hangs.
- **`UV_SYSTEM_CERTS: "1"`** in the env block — see traps.

Restart with **File → Exit**, not by closing the window. Verify with a
trivial read; if it returns data, the loop is live.

## The hang: corrected model

**Superseded, 2026-08-18.** This doc previously described a hang as the
*server* wedging — "a write hangs, then all calls including reads time out
until both sides are restarted, and the write usually landed." **That model
is wrong** and following it wastes two restarts and a conversation.

What the add-on log actually shows during a hang: **nothing arrives**. No
`CallToolRequest`, no stalled processing. The server sits idle and healthy
for the whole interval, then serves the next call normally the moment the
client's ~4-minute timeout expires. The request dies before reaching Home
Assistant.

Three consequences:

- **Do not restart anything.** All three hangs observed in one session
  cleared on their own. The server was never wedged.
- **Do not assume the write landed.** It did not, on either occasion it was
  checked. Verify before re-issuing — but the old advice to assume success
  and fear double-application is backwards.
- **Retry is the correct response**, and it is cheap.

### ha_set_device is reproducibly broken

Hangs on every attempt — twice on an identical call.
`ha_remove_helpers_integrations` hung once. Meanwhile `ha_set_entity`,
`ha_config_set_script`, `ha_call_service`, `ha_reload_core`, the `ha_get_*`
family and template evaluation are all reliable, including a large
multi-branch script transform.

**Workaround:** route device-level changes through `ha_set_entity` where the
same outcome is reachable (entity area assignment instead of device area
assignment). Otherwise use the HA UI.

**Unexplained:** why those tools and not others. The "both were loaded via
`tool_search` immediately before use" theory does not survive — so was
`ha_config_set_script`, which works. Worth an `ha_report_issue` filing if it
recurs.

## Other traps, with evidence

**`invalid peer certificate: UnknownIssuer`.** Antivirus or a proxy doing
HTTPS inspection breaks uv's TLS when fetching `fastmcp-remote` from PyPI.
Fix is `UV_SYSTEM_CERTS` in the env block. `UV_NATIVE_TLS` is the deprecated
name for the same thing. The error's own hint suggests a `--system-certs`
flag; the env var is the version that works through Desktop's config.

**`uvx` not recognized in PowerShell.** uv installs to the user profile. An
**Administrator** PowerShell does not inherit that PATH entry. Test in a
normal shell. This is not a broken install — Claude Desktop finds it either
way.

**The add-on's welcome page proves less than it looks.** It is static. It
confirms the port is reachable and nothing else.

**Ignore the welcome page's troubleshooting advice.** Cloudflare
bot-blocking, geo-blocking, Anthropic egress ranges: all of it applies to
*cloud-brokered* connectors only. With local stdio through `fastmcp-remote`,
nothing from Anthropic's network touches HA.

**HA is not necessarily on 8123 here.** Read the port from `ha network info`
at the console rather than assuming. Getting this wrong reads as "HA is
down".

**"MCP is Desktop-only" is not reliable.** This doc previously stated it as
fact. A session that was assumed to have no tools turned out to have them.
**Probe for tool availability rather than inferring it from this doc.**

**`BestPracticeKey` rotates hourly.** Re-read it from the skill guide
immediately before each write call that requires it. A stale key fails the
write.

**`config_hash` must match at write time.** Read it immediately before
modifying a script; a hash from earlier in the session may be stale. This is
also the concurrency guard if something else is editing the same object.

## Debugging playbook

Work outward from HA. Each step rules out one layer.

1. **Is HA itself up?** Load the web UI. Slow or dead → the problem is the
   VM. Check for IP drift at the VirtualBox console.
2. **Is the add-on running, and what does its log say?** Highest-yield
   single check. Crucially, this is also what distinguishes a *client-side*
   hang (no request logged at all) from a genuine server stall (request
   logged, never completes). Do this before restarting anything.
3. **What does Claude Desktop's own log say?** Healthy startup runs through
   `initialize` → FastMCP banner → `tools/list` → result lines. If it stops
   before `tools/list` returns, the proxy never came up — read the error
   immediately above.
4. **Can uvx fetch the proxy at all?** In a non-admin shell, run
   `uvx fastmcp-remote --help` with `UV_SYSTEM_CERTS` set. Help text means
   the download path works and packages are cached.

### Symptom → cause

| Symptom | Likely cause |
| --- | --- |
| Single call times out ~4 min, others fine after | Client-side stall. Retry. Do **not** restart. Verify the write didn't land. |
| `ha_set_device` times out | Known broken. Use `ha_set_entity` or the UI. |
| `invalid peer certificate` | AV HTTPS inspection → set `UV_SYSTEM_CERTS` |
| `uvx` not recognized | Admin PowerShell missing user PATH |
| Server not listed under Developer | JSON syntax error, or config in wrong path |
| Connects then disconnects | Read the log line above the disconnect |
| Hangs waiting on OAuth | Add `"--auth", "none"` before the URL |
| Write rejected, no obvious reason | Stale `BestPracticeKey` or stale `config_hash` |

## How to audit this

Everything stripped from this doc is one call away:

- **Add-on slug, version, state, resource use** — `ha_get_addon(include_stats=True)`
- **Add-on log** — `ha_get_logs(source="supervisor", slug=…)`
- **HA version, disk, host, network, repairs** — `ha_get_system_health(include="repairs")`
- **Is the loop live** — `ha_eval_template("{{ now() }}")`
- **Current `BestPracticeKey`** — `ha_get_skill_guide(...)`, re-read per write

## Fallback options

Ranked by effort:

1. **Screenshots into chat.** Slow, always works.
2. **Claude Code against `/config` over Samba.** Better than MCP for YAML —
   automations, `intent_script`, custom sentences, trigger-based templates —
   since it edits files directly.
3. **Cloud-brokered connector.** Works across web, desktop and mobile, but
   needs HA publicly reachable via Cloudflare Tunnel or Nabu Casa. Tailscale
   cannot serve this — it is private by design, and the broker has to reach
   the instance.

## Standing hygiene

- **DHCP reservations for every speaker and the HA VM.** No longer
  hypothetical: IP drift took a speaker out of Music Assistant for an
  unknown period and was the direct cause of a live outage. Do this while
  there are few devices.
- **Backups before structural changes.**
- **Bridge over Ethernet, never Wi-Fi.** Switching fixed hanging add-on
  installs, wedged Supervisors, simultaneous player dropouts, streaming
  audio skipping, and apparent rate limiting — five distinct-looking bugs,
  one cause. If several unrelated symptoms appear at once, look for a shared
  dependency first.
- **Never hard power-off the VM.** Use `ha host shutdown`, the UI power
  icon, or ACPI Shutdown.
