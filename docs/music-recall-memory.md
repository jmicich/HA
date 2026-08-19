# Music recall memory — design

Status as of 2026-08-19. **Built and working end to end for logging, and
the prompt block is now live too — partially effective, see Verified below.
Warm start remains.**

Read alongside `voice-and-music.md` (three-layer split, the script, the
traps).

**No auditable state recorded here** — no entity IDs, config hashes, or list
contents. See "How to audit this".

## Problem

Requests arrive garbled — partial titles, mangled artist names, kid-speak.
The search layer resolves them to *something* regardless (defect #3, still
open), so the failure is silent rather than loud.

A list of what has actually played in this house is a strong prior for
interpreting a mangled request. Fuzzy matching then happens inside the
model, which is what it is good at.

**Scope: music only.** Deliberately not general-purpose assistant memory.

## Decision

1. `script.play_music` fires a `music_played` event on each successful
   branch, carrying the `result` it already builds.
2. An automation listens for it and writes a prepend-capped, deduped list
   into an `input_select` via `input_select.set_options`.
3. The conversation agent prompt injects that list on every turn via
   `state_attr(…, 'options')`.

No embeddings, no vector store, nothing new running on the VM.

## Why input_select, and not the obvious things

The read path is the binding constraint: **the agent prompt is a Jinja
template, and templates cannot call services.** The store must therefore be
template-readable. That eliminates most candidates.

| Option | Verdict |
| --- | --- |
| Trigger-based template entity holding a list attribute | **The original plan; not buildable via MCP.** The UI template helper is state-based only — no trigger, no `action:` block, **and no `attributes` field**. Trigger-based templates are YAML-only, and `/config` is not writable from MCP without HACS. |
| todo list | Not template-readable. `todo.get_items` is a service, so it could only be exposed as a *tool* — an optional extra round trip the model may skip. |
| `input_text` | 255-character cap. Roughly eight entries. A multi-helper ring buffer works but looks like a mistake to whoever reads the config later. |
| General memory integration (embedding-backed) | Solves a bigger problem than we have; adds an embedding backend to a CPU-only VM. Revisit if non-music memory becomes a goal. |
| Replacement conversation agent with a vector store | Throws away the existing agent setup and the three-layer split. |
| MA playlog as the store | Not template-readable either, and library-only. Retained as warm start. |

**The design was recommended once before this constraint was checked**, and
had to be thrown away. Check what the tooling can create *before* designing
around it.

## Verified behaviour

Tested against a throwaway helper.

| Check | Result |
| --- | --- |
| `set_options` writes an arbitrary list at runtime | Works |
| Template read of the options attribute | Works |
| Prepend + cap at 40 | Works |
| Dedupe — a repeat play moves to front, no duplicate | Works |
| Survives a helper reload | **Survives** |
| Currently-selected option evicted by the cap | Falls back to first option, no error |
| 40 realistic-length titles | Accepted intact, no truncation |
| `set_options` accepts a template rendering to a list literal | **Works** — confirmed by the live automation |

**The reload result is the important one.** A reload re-reads from the
storage collection, so values surviving it means `set_options` persisted to
`.storage` rather than only mutating memory. A restart reads the same store,
so restart persistence is near-certain — **but this is an inference, not a
measurement.** Confirm at the next natural restart.

**Two side effects, both acceptable:** the entity's *state* is the selected
option, so it changes on every write (one recorder row per play, slightly
odd entity in the UI); and `set_options` silently resets the selection to
the first item, which nothing reads.

Working accumulate expression, rendered against live state — newest first,
deduped, placeholder rejected, capped:

```jinja
{{ ([new] + <current options> | reject('eq', new)
     | reject('eq', '(none)') | list)[:40] }}
```

## Built

| Thing | State |
| --- | --- |
| The `input_select` helper | **Created** |
| Logging automation | **Created**, `mode: queued`, fires on the event, guards against empty payloads |
| `script.play_music` event fires | **All ten playback branches**, inserted between the result assignment and the stop |
| End-to-end chain | **Verified** — a real playback call resolved, fired, logged, and appeared at the front of the list |
| Warm start | Not built |
| Prompt block | **Built 2026-08-19** — see Verified behaviour below; effectiveness is condition-dependent, not a complete fix |

The script edits used `python_transform` keyed on branch aliases, with an
idempotency guard so a re-run does not double-insert. The `default:` branch
sets no result and was correctly left untouched.

**Coverage caveat:** the end-to-end test exercised one of the ten branches.
The transform applied uniformly and the guard makes it safe to re-run, but
nine branches are inferred-good, not verified-good. The regression suite
would exercise the rest.

**Cleanup outstanding:** a throwaway test helper was created during
verification and its deletion was issued during a client-side hang. Confirm
whether it still exists before assuming it is gone.

## Remaining work

### 1. Warm start

At `homeassistant.start`, seed from MA's playlog via
`music_assistant.get_library` with `order_by: last_played`, so the list is
not empty after a reboot.

**Map to plain strings before use** — the enum stringification trap has
bitten twice.

Two known limits, neither on the critical path: the playlog covers library
items only, and there is an upstream report of plays to some speakers not
being recorded at all. If that is still broken, warm start degrades; the
feature does not.

### 2. Prompt block — built 2026-08-19

Added to the conversation agent's system prompt as its own section,
substantially as originally specced:

```jinja
Recently played in this house: {{ (state_attr('input_select.music_recall', 'options') | default([], true)) | join(', ') }}

Household members, including children, often ask for music with imperfect
titles or artist names. If a request closely resembles one of those recent
titles, treat that exact title as the query instead of the words the user
actually said. If the request does not closely resemble anything in that
list, ignore the list entirely and treat it as new.
```

This is **not** the trap `voice-and-music.md` warns about. That trap was
hardcoding facts HA already knows and letting them go stale. This is a
derived, live value — the same pattern as deriving rooms from visible media
players.

**Verified behaviour, condition-dependent — not a complete fix:**

| Garbled reference | Real title | Result |
| --- | --- | --- |
| "Take 5" | "Take Five" | **Matched.** Trace confirmed `query: "Take Five"` — the model substituted the canonical title, not the words spoken. |
| "Roomers" | "Rumours" | **Not matched**, on a clean list (retested after removing pre-existing pollution — see trap below). Trace confirmed `query: "Roomers"` passed verbatim. |

The difference tracks textual distance, not intent clarity: "Take 5" and
"Take Five" differ by one easy, common substitution (digit vs. spelled
number). "Roomers" and "Rumours" are a plausible **phonetic** collision —
this pairing was chosen specifically to simulate an STT mishearing — but
the model works from already-transcribed text, where the two words look
fairly different (`roomers` vs `rumours`). The prompt block helps close
textual gaps; it doesn't reliably close phonetic-only gaps once the
transcription has already flattened them to different-looking text. Whether
that's worth chasing further depends on how often real garbling looks more
like the STT case than the digit-substitution case — no evidence yet either
way.

**New trap, found while verifying this: a wrong resolution that gets logged
becomes self-reinforcing.** Testing defect #3 before this prompt block
existed produced two wrong resolutions ("Roomers" instead of Rumours, "Take
5" instead of Take Five) that got logged into the recall list like any other
play, since the logging automation has no way to know a play was a mistake.
With the prompt block live, a future *identical* mangled request would then
literally string-match the wrong logged title rather than the correct one —
confirmed directly: the first post-fix retest of "Roomers" still failed
because "Roomers" itself was sitting in the list as an exact match,
out-competing "Rumours". Cleaned up by hand this time
(`input_select.set_options`); nothing in the design currently prevents this
from recurring on a real wrong resolution. Worth a mitigation if wrong
resolutions turn out to be common enough to matter.

## Known design gap: no artist in the payload

`result` carries the resolved item's **name only** — a bare title, no
artist. So the recall list cannot help with a mangled *artist* name, which
was half the original use case.

The fix is enriching the event payload with artist at the point of
resolution. Worth deciding **before** the list accumulates a few hundred
entries in the wrong shape, since old entries will not be backfilled.

## The failure mode to test for

**Over-anchoring.** A list of past plays gives the model something to snap
to, and it may bend a genuinely new request onto a previous play. Inverse of
defect #3, and the thing most likely to make this a net loss. Both recall
cases in the regression suite need ≥3 runs.

## Not solved by this

- **Defect #3 (no relevance threshold).** Nonsense queries still fuzzy-match
  to real content, and a recall list arguably gives them one more thing to
  match against. Still needs a threshold in the script.
- **Per-person memory.** The voice satellite does not identify speakers. One
  shared household list; requests cannot be separated by person.

## How to audit this

- **Does the helper exist, and what does it hold** — read its `options`
  attribute
- **Is the automation live and firing** — its `last_triggered` attribute,
  which should track the most recent play
- **Are the event fires in the script** — `ha_config_get_script`; each
  playback branch should carry an `event:` action between its result and its
  stop
- **Is the chain working** — call the script directly, then re-read the
  options list; the resolved name should be at the front
- **MA config entry ID** for the warm-start call — `ha_get_integration`
