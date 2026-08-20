# Music recall memory — design

Status as of 2026-08-19. **Built end to end: logging and the prompt block
are both live. Warm start remains. The prompt block only partially works —
see "Verified behaviour of the prompt block" below before assuming it
covers what the name suggests.**

Read alongside `voice-and-music.md` (three-layer split, the script, the
traps).

**No auditable state recorded here** — no entity IDs, config hashes, or list
contents. See "How to audit this".

## Problem

Requests arrive garbled — partial titles, mangled artist names, kid-speak.
The search layer resolves them to *something* regardless (defect #3 in
`voice-and-music.md`; 3a is fixed, 3b and 3c remain open — see there), so
the failure is silent rather than loud.

A list of what has actually played in this house is a strong prior for
interpreting a mangled request. Fuzzy matching then happens inside the
model, which is what it is good at.

**Scope: music only.** Deliberately not general-purpose assistant memory.

## Decision

1. `script.play_music` fires a `music_played` event on its playback path,
   carrying the `result` it already builds.
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
| `script.play_music` event fires | **Yes**, inserted between the result assignment and the stop. Was "all ten playback branches" before the 2026-08-19 search/play split collapsed them to one. |
| End-to-end chain | **Verified** — a real playback call resolved, fired, logged, and appeared at the front of the list |
| Warm start | Not built |
| Prompt block | **Built** — see below for what it does and doesn't fix |

The script edits used `python_transform` keyed on branch aliases, with an
idempotency guard so a re-run does not double-insert. The `default:` branch
sets no result and was correctly left untouched.

**Coverage caveat, historical:** at the time this was written, `play_music`
had ten playback branches and only one had been end-to-end verified. Moot
since the 2026-08-19 search/play split collapsed all ten into a single
playback path, which the regression suite has since exercised repeatedly.

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

## Prompt block — built, wording as deployed

Wording is the tool schema — treat as code. Frames the list as a *hint*,
injected into the conversation agent's prompt on every turn:

```jinja
Music, matching a mangled or partial request to something recently played:
- Recently played in this house: {{ (state_attr('input_select.music_recall', 'options') | default([], true)) | join(', ') }}
- Household members, including children, often ask for music with imperfect
  titles or artist names. If a request closely resembles one of those recent
  titles, treat that exact title as the query instead of the words the user
  actually said.
- If the request does not closely resemble anything in that list, ignore the
  list entirely and treat it as new.
```

This is **not** the trap `voice-and-music.md` warns about. That trap was
hardcoding facts HA already knows and letting them go stale. This is a
derived, live value — the same pattern as deriving rooms from visible media
players.

### Verified behaviour of the prompt block

Tested both before and after the `search_music`/`play_music` redesign
(`voice-and-music.md`) — the split didn't change the outcome, which is the
important result here.

| Case | Result |
| --- | --- |
| "Take 5" → "Take Five" | **Fixed.** Textually close — one common digit/spelled-number substitution. Model reliably substitutes the canonical title, verified via trace. |
| "Roomers" → "Rumours" | **Still fails, deterministically (0/3, 2026-08-19).** Phonetically close but textually different once transcribed. Fails the same way every time, not intermittently — see the trap in `voice-and-music.md` ("Having the right answer in context does not mean the model uses it"). The correct title was present in the recall list *and* independently in `search_music`'s own candidates on every attempt; the model still picked the literal-text match ("Roomers", a real playlist). |

**The pattern is textual distance, and it did not move when the pipeline
changed underneath it.** The block closes an easy substitution gap but not
a phonetic-only one — and giving the model a second, independent path to
the same correct answer (real search candidates, correctly artist-tagged)
didn't close it either. This looks less like "not enough signal" and more
like literal text similarity dominating regardless of how much correct
signal sits alongside it. No fix identified; flagged as open in
`voice-and-music.md` defect 3b.

### Self-reinforcement trap

A wrong resolution that gets logged into the recall list becomes a future
exact-match target for the same wrong request — confirmed directly: an
earlier failed "Roomers" test logged "Roomers" itself into the recall list,
which then out-competed the correct "Rumours" entry on the next attempt
purely because it was an exact string match. **A wrong resolution is not
self-correcting; it's self-reinforcing.** The list needs occasional manual
cleaning of known-wrong entries — there is no automatic mechanism to detect
or evict one. Nothing currently prevents this recurring on a genuinely new
wrong resolution, not just the "Roomers" one already seen twice.

## Known design gap: no artist in the payload — closed 2026-08-19

`result` used to carry the resolved item's **name only** — a bare title, no
artist — so the recall list couldn't help with a mangled *artist* name,
which was half the original use case.

**Closed as a side effect of the `search_music`/`play_music` redesign**
(`voice-and-music.md`): `play_music` now has an `artist` field, populated
from the chosen `search_music` candidate, and the `music_played` event
carries it alongside `played`/`kind`. Not yet used to enrich the recall
*list itself* (the list still stores bare titles) — that's a separate,
still-open decision: whether to change the stored format (and how to
migrate or accept that old entries won't backfill).

## The failure mode to test for

**Over-anchoring.** A list of past plays gives the model something to snap
to, and it may bend a genuinely new request onto a previous play. Inverse of
defect #3, and the thing most likely to make this a net loss. Both recall
cases in the regression suite need ≥3 runs.

## Not solved by this

- **Defect #3a (title collisions) — solved, but by `voice-and-music.md`'s
  search/play redesign, not by this.** Recall and search-candidate judgment
  are independent mechanisms that happen to both have pointed at the right
  answer in the one case tested ("Roomers") and it still wasn't enough —
  see "Verified behaviour of the prompt block" above.
- **Defect #3b (phonetic/garbled-recall mismatches) — still open.** This is
  the case this feature was partly built for, and it's the one still
  failing. Nonsense queries also still fuzzy-match to real content, and a
  recall list arguably gives them one more thing to match against. Still
  needs either a non-textual matching approach or a threshold in the
  script — a better prompt has been tried and didn't close it.
- **Per-person memory.** The voice satellite does not identify speakers. One
  shared household list; requests cannot be separated by person.

## How to audit this

- **Does the helper exist, and what does it hold** — read its `options`
  attribute
- **Is the automation live and firing** — its `last_triggered` attribute,
  which should track the most recent play
- **Is the event fire in `play_music`** — `ha_config_get_script`; the
  single playback path should carry an `event:` action between its result
  and its stop. (Before the 2026-08-19 search/play split this was ten
  branches in one script, each needing the same check — now there's one
  path to check.)
- **Is the chain working** — call the script directly, then re-read the
  options list; the resolved name should be at the front
- **MA config entry ID** for the warm-start call — `ha_get_integration`
