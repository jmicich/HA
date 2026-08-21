# Prompt caching — why it matters here, and the two ways to get it

Status as of 2026-08-21. **Pinned:** the OpenRouter route below is designed
and half-built but deliberately not finished. The Anthropic route is the one
in use.

Read alongside `general-knowledge-search.md`, which contains the measurements
that led here.

## The problem

The tier-1 agent sends a large, near-identical prefix on **every utterance in
the house** — instructions, tool schemas, and the exposed-entity surface.
Roughly 6,000 tokens, of which only a few hundred change between requests.

Cached input costs a fraction of fresh input, so this is the exact shape
prompt caching exists for. It is also the only optimisation available that
removes *nothing*: trimming prose costs information, caching costs none.

**It is a cost optimisation, not a latency one — see "Measured result".**
That distinction was not obvious in advance and is the main thing to carry
away from this document.

**Measured cache hit rate before any of this: 0.0%**, across hundreds of
requests. Nothing was being cached at all.

## Why it was zero

Not a misconfiguration. Home Assistant's `open_router` integration builds its
request with an `extra_body` carrying only `require_parameters` and tools, and
sets no `cache_control` anywhere. The provider supports caching; the
integration never asks for it.

Three dead ends, checked so they are not re-explored:

| Attempted route | Why it fails |
| --- | --- |
| An OpenRouter account setting | There isn't one. Preferences, Routing and Presets were all checked. |
| A provider-side **preset** carrying the parameter | `cache_control` is a per-message annotation, not a request parameter, so a preset cannot supply it. |
| Referencing a preset from the model field | The integration's model field is a validated dropdown that rejects arbitrary values. |

Caching requires the *client* to send `cache_control`, and the only component
in a position to do that is the integration.

## Route A — Home Assistant's own Anthropic integration (in use)

HA ships an `anthropic` integration that has supported this all along. It
sets `cache_control` on the system block, exposes the behaviour as a
configuration option (`off` / `prompt` / `automatic`), and **defaults to on**.

It also carries two things the OpenRouter subentry does not expose at all: a
`max_tokens` control, and a web-search option with structured location fields
(city, region, country, timezone) that would replace the
coordinates-in-the-prompt workaround described in
`general-knowledge-search.md`.

**The trade, stated plainly:** this runs against the multi-provider goal in
`project-overview.md`. OpenRouter was chosen deliberately as the portal to
many providers; going direct to Anthropic narrows that to one vendor and
needs a second API key with its own billing.

**A trap worth knowing:** the subentry's config flow only exposes three
fields (`prompt`, `llm_hass_api`, `recommended`) through the API. Everything
else — including the caching option — lives in a later flow step and is
invisible to schema introspection. The stored values are real and persist
across updates, but confirming them means reading the config entry itself,
not the schema. See "How to audit this".

### Measured result

After migrating tier 1 and running normal traffic, the provider's own usage
breakdown split input tokens roughly:

| Token type | Share of input |
| --- | --- |
| Cache **read** | ~80% |
| Cache **write** | ~10% |
| Fresh input | ~10% |

**Against a measured 0.0% before.** Cache reads bill at a fraction of fresh
input, so this is a large cost reduction — and unlike the prompt trimming
that preceded it, it removed nothing.

Applying the published multipliers — cache read 0.1x, five-minute cache
write 1.25x, fresh input 1x — that split bills like **~30% of the input cost
it would otherwise incur**, a reduction of roughly 70%. Output is unaffected,
so the saving on a whole request is smaller than that.

**The break-even is the number that actually matters**, because cache writes
cost *more* than not caching at all. For one write followed by N reads over
the same prefix, caching wins when `1.25 + 0.1N < N + 1`, i.e. **N > 0.28**.

In plain terms: a single isolated command pays ~25% more than it would
without caching, and **any second request inside the cache lifetime already
puts you ahead** — steeply so from there.

That makes the measured 80% read share less impressive than it first looks:
it was gathered during rapid testing, which is the best case for caching.
Real household use is sparser, and a house that only ever issues one command
every half hour would be *paying a penalty*, not saving. Worth re-measuring
the read/write split against genuine usage before treating the 70% as real.

### Negative result: no latency improvement

Caching was pursued partly for speed. **It did not deliver any.** Tier-1
decision latency, measured as the gap between the conversation starting and
the first tool firing, on the same phrasing within the same few minutes:

| | median | range |
| --- | --- | --- |
| Without caching | ~1.8s | 1.2–4.6s |
| With ~80% cache reads | ~2.2s | 1.8–3.3s |

Both are dominated by variance. The medians differ by less than the spread,
so this shows no improvement and no regression — it shows *no effect*.

**The reason, which should have been reasoned out beforehand:** prompt
caching does not reduce what is sent. The whole prompt still uploads on every
request; the provider skips *recomputing* over it. The saving is compute and
billing, not transfer. At a few thousand tokens the prefill was never the
bottleneck — network round trip and output generation dominate, and caching
touches neither.

**Method note, and a trap.** The first comparison used caching numbers from
one day against non-caching numbers from the previous day, and appeared to
show caching making things *2x slower*. Re-running both against the live
system minutes apart erased the difference entirely. At this variance,
cross-session latency comparisons are worthless; measure both arms together
or not at all.

**Where the latency actually is:** an escalated question is three sequential
model calls, and nothing here changed that. See `general-knowledge-search.md`,
which separately found that *removing* the escalation hop does not help
either, because the search round trip dominates.

## Route B — patch the OpenRouter integration (pinned)

This is the route that keeps the multi-provider flexibility *and* gets
caching. It is two pieces of the same work.

### B1. The upstream contribution

A patch adding an opt-in `prompt_caching` boolean to the OpenRouter
conversation subentry, setting OpenRouter's top-level automatic form:

```
extra_body["cache_control"] = {"type": "ephemeral"}
```

Top-level rather than hand-placed per-message breakpoints, because OpenRouter
advances the breakpoint itself as the conversation grows — which suits a
fixed prefix plus growing history. It requires ~1,024 tokens minimum to
engage; this prompt is far above that.

Opt-in rather than always-on, so no existing user's cost or behaviour changes
silently. **The strongest argument for the change is that the `anthropic`
integration in the same codebase already has exactly this option** — it is
bringing one integration in line with accepted prior art, not proposing a new
idea.

**State: written, committed to a fork branch, deliberately not submitted.**
Three things block an honest submission:

1. **The tests have never run.** Home Assistant core does not support Windows
   for development and this machine has no WSL, so only `ruff` could be run
   (clean). The test is written against the neighbouring `web_search` test's
   exact shape but is unexecuted. The PR checklist asks the contributor to
   attest that local tests pass.
2. **A documentation PR is required** against `home-assistant.io`, because
   this adds a user-exposed configuration option.
3. **Their AI policy requires the PR description be written by the
   contributor in their own words**, and forbids using AI to answer
   maintainer questions. Generated prose pasted into the description is
   grounds for closing the PR. This is a hard constraint on how the
   submission gets written, not a formality.

### B2. Vendoring it locally, which is also how B1 gets validated

`open_router` is built into core, but Home Assistant lets
`custom_components/<domain>/` **override** a built-in of the same domain. The
patched integration can therefore run in this house before it is merged
anywhere.

This is not merely a workaround. The patch has never executed; running it in
daily use converts the PR from an untested diff into "measured hit rate went
from 0% to N%, in production since <date>", which is a different quality of
evidence.

**The implementation is identical to B1 — same patch, same files.** Only
deployment differs, in exactly three ways:

| | Upstream PR | Vendored copy |
| --- | --- | --- |
| The four integration files | identical | identical |
| Tests | shipped | not deployed |
| `manifest.json` `version` field | not used | **required** |
| `translations/en.json` | generated at release | **must be built by hand** |

That last row is the non-obvious one. `loader.py` gates on a `translations/`
**directory** existing and never reads `strings.json` at runtime; core
generates the directory during release and `.gitignore` excludes it, so no
component carries one in git. A vendored copy shipping only `strings.json`
loads fine and then renders raw translation keys in the UI instead of labels.
The `[%key:...%]` references resolve against core's own top-level
`strings.json`.

### What vendoring costs

**Shadowing a core integration freezes it.** Upstream fixes and features stop
arriving — including, eventually, this very feature once merged. Two sharper
risks: if core changes an internal API the integration uses, a frozen copy
breaks, possibly subtly rather than at startup; and the manifest's pinned
requirements resolve independently of core's, so they can drift into
conflict.

Mitigation is a drift check alongside `seed_config` / `sync_config` /
`export_ha`, comparing the vendored copy against what core ships, run at
every Home Assistant upgrade.

**Checked and true as of writing:** the integration is byte-identical between
the installed version and current `dev`, so the patch applies cleanly to what
is actually running. Re-check this before starting — it is the assumption the
whole approach rests on.

## Sequencing

1. **Now:** Route A. Caching works today; no maintenance burden.
2. **Later:** vendor B2, measure the hit rate over real use.
3. **Then:** submit B1 with those numbers, written up personally.
4. **After merge:** delete the custom component, return to the built-in
   integration, and get multi-provider flexibility *with* caching — which is
   the actual goal. Route A becomes unnecessary at that point.

**Revisit trigger:** either the multi-provider goal becomes active again, or
the Anthropic-only dependency becomes uncomfortable. Until one of those, the
pin holds.

## How to audit this

- **Whether caching is actually happening** — the provider's own dashboard
  reports a cache hit rate. It is the only trustworthy signal; configuration
  saying caching is enabled is not evidence that it engaged.
- **What the Anthropic subentry is really configured with** — read the config
  entry's stored data, not the config-flow schema, which hides most fields
- **Whether the vendored copy has drifted** — diff it against the integration
  shipped by the running Home Assistant version
- **Whether the upstream patch still applies** — compare the integration
  between the installed version tag and `dev` before resuming
