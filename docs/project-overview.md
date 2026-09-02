# Home Assistant build-out — project overview

Status as of 2026-08-21. Orientation doc: read this first, then the
feature-specific docs (`voice-and-music.md`, `music-recall-memory.md`,
`assistant-memory.md`,
`general-knowledge-search.md`, `prompt-caching.md`, `dev-environment.md`,
`wake-word-findings.md`, `stt-endpointing.md`).

## Documentation principle

**These docs do not record state that can be obtained by auditing the
system.** No entity IDs, speaker inventories, room assignments, IP
addresses, version numbers, config hashes, or pass/fail measurements. Each
doc carries a "How to audit this" section listing the calls that regenerate
that information on demand.

The reason is empirical. Recorded state goes stale silently, and a stale
fact is worse than a missing one: it gets trusted. Across sessions,
essentially every wrong diagnosis here traced back to trusting a written
fact instead of reading the system — a speaker's room, a speaker inventory,
which agent was live, whether a vendor app had been configured, whether the
tooling had a given capability. Meanwhile the traps and rationale in the
same docs have held up consistently.

This is the same rule the design already applies one level down: *do not put
facts in the prompt that HA already knows*. It applies to the docs too.

**What stays:** design intent, decisions and why they were made, traps with
the evidence behind them, negative results, and anything an audit
structurally cannot produce — an audit can tell you a room has no speaker,
but not whether that is deliberate.

**What also stays:** identifiers that are load-bearing *inside code* (a
config entry ID embedded in a service call), marked explicitly as "read this
before use" rather than quoted.

**Accepted cost:** recorded state doubles as a drift tripwire. A live outage
was noticed only because a doc asserted something that no longer matched.
Removing that trades a rare true signal for a frequent false one — judged
worth it, but not free.

## Goal

A conversational smart home with real reasoning, not keyword matching.
Three tiers, cheapest capable layer first:

| Tier | Handles | Status |
| --- | --- | --- |
| 0 — local intents (hassil) | High-frequency exact phrases. Free, instant. | Exists, off |
| 1 — cheap LLM | Intent extraction, entity resolution, synonyms, context | Live |
| 2 — heavy model | Research, multi-source correlation, deep workflows | **Live for general knowledge and web search** |

Tier 0 → 1 routing is the built-in `prefer_local_intents` pipeline flag, not
custom code. Tier 1 → 2 has no native mechanism; it is a script exposed to
Assist as a tool, which the tier-1 model calls when a task exceeds it.

**The script-as-tool pattern is proven** — `script.play_music` established
it, and tier 2 now uses it: the escalation script calls
`conversation.process` against a second, stronger conversation subentry that
has web search and no house access. See `general-knowledge-search.md` for
the design, its suite, and the traps.

Tier 2 currently covers general knowledge and current events only.
Multi-source correlation and deep workflows remain unbuilt.

## Platform constraints

These are load-bearing. Several plausible designs are ruled out by them.

- **HA runs in a VirtualBox VM**, x86_64, no GPU, on a Windows host, bridged
  over Ethernet.
- **No GPU passthrough.** VirtualBox does not practically support it;
  getting a GPU into this VM means migrating hypervisors. **Don't.** Run any
  inference server on another LAN box and point the integration at its URL.
  Bridged networking means the VM already reaches the host directly.
- **Local LLM on this host is not viable.** CPU inference adds seconds per
  turn, which is fatal for voice.
- **Disk is the tightest resource.** Roughly a third free. Anything multi-GB
  (ESPHome toolchains, embedding backends, vector stores) needs a budget
  check first.
- **HA Cloud has an expiry.** The preferred pipeline uses it for STT and
  TTS. Local fallback is installed and configured, but slower on this
  hardware. Audit the expiry date rather than trusting a written one.

## LLM provider routing — findings

**The built-in OpenAI Conversation integration cannot use a proxy.** Its
docs are explicit: official endpoint only, no OpenAI-compatible third
parties, no LiteLLM, no local vLLM. An earlier plan to use it as a swappable
socket behind a proxy was wrong and has been abandoned.

Supported paths for the multi-provider goal:

- **OpenRouter** — built-in integration, HA's own recommended alternative
  for other providers. One key, many models. Closest fit to the original
  "portal to various LLM providers" idea.
- **Ollama** — built-in, takes a URL. The path for local models on a GPU box.

No custom integration or proxy layer is required for either.

**The multi-provider goal is currently on hold for the tier-1 agent.** It
runs on the direct Anthropic integration instead, because that one supports
prompt caching and OpenRouter's does not — a difference worth several
thousand tokens on every utterance in the house. This is a deliberate,
reversible narrowing, and the route back to OpenRouter *with* caching is
designed and half-built. See `prompt-caching.md`, which also records why no
amount of OpenRouter-side configuration can close the gap.

## Current shape

Rather than an inventory: only part of the house is populated with speakers,
and more hardware is gated on this phase showing promise. **The main limit
on usefulness is the number of entities exposed to Assist, not model
capability** — the assistant can only act on what it can see.

Audit the exposure surface before concluding the assistant is
underperforming.

## Open items

Roughly by leverage:

1. **DHCP reservations for every speaker and the HA VM.** No longer
   speculative: IP drift silently removed a speaker from Music Assistant and
   was the direct cause of a live outage, after being a suspected cause in
   three earlier incidents. Do this while there are few devices.
2. **Expose real entities** as hardware arrives. Everything else is
   bottlenecked on this.
3. **Area aliases.** None set. Helps both tiers, cheap to add.
4. **Extend tier 2 beyond Q&A** — the escalation script exists and works
   (`general-knowledge-search.md`); multi-source correlation and deep
   workflows are the unbuilt part.
5. **Custom local intents** once the entity surface is stable. These are
   files in `/config`, which MCP cannot write — needs File editor, SSH, or
   Samba. Note also that the LLM agent does not support HA *sentence
   triggers*; custom sentences are a separate, working mechanism.
6. **Turn on `prefer_local_intents`** — only after re-testing music end to
   end. See `voice-and-music.md` for why it silently breaks playback.
7. **Music recall: warm start and prompt block** — see
   `music-recall-memory.md`.

**Deliberately not chasing:** the missing model selector in the OpenAI
integration UI (upstream bug; OpenRouter is the alternative path).

## Working agreements

- **Probe, don't infer.** State, logs, and execution traces before
  diagnosis. This applies to *the tooling* as much as the system — several
  wrong calls came from assuming a capability existed or didn't without
  checking.
- **Test closed-loop:** `conversation.process` with `return_response: true`,
  then read traces. Don't ask the human to speak to a device.
- **Verify current config before changing it**; this system is in active flux.
- **Never check a live answer against your own recollection.** An assistant
  working here has a knowledge cutoff older than the house's current date.
  When a time-sensitive answer contradicts what you "know", you are the
  suspect party — verify with a narrow, dated search instead. This rule was
  written after an extended investigation into a hallucination bug that did
  not exist; see `general-knowledge-search.md`.
- **Record durable decisions in these docs** rather than leaving them in
  chat — but record *decisions*, not *state*.
- **When a diagnosis turns out wrong, say so plainly and move on.**

## How to audit this

- **Integrations and config entries** — `ha_get_integration`
- **Add-ons, versions, resource use** — `ha_get_addon(include_stats=True)`
- **HA version, disk, host, network, repairs** — `ha_get_system_health(include="repairs")`
- **Areas and floors** — `ha_list_floors_areas`
- **What is exposed to Assist** — `ha_get_entity_exposure`
- **Pipelines, agents, wake words** — `ha_manage_pipeline`
- **Pending updates** — `ha_manage_updates`
