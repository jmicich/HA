# Assistant memory — design

Status as of 2026-08-31. General-purpose memory for the voice assistant:
durable facts about this house and the people in it, injected into every
tier-1 conversation.

Read alongside `music-recall-memory.md`, which is the **music-only**
predecessor and deliberately not this. That document ends its rejected-options
table with "Revisit if non-music memory becomes a goal" — this is that
revisit, and it reaches a different conclusion on one point because a
constraint changed.

**No auditable state recorded here** — no entity IDs, helper contents, or
current memory entries. See "How to audit this".

## Scope, decided up front

| Axis | Decision |
| --- | --- |
| Content | Durable facts and preferences only. Not routines, not conversation history. |
| Write | Explicit only — someone must ask. No automatic capture. |
| Read | Injected into the tier-1 prompt on every turn. Not a tool. |
| Store | `input_select`, no restart required. |
| Cap | 12 entries. |

## The constraint that changed, and the one that did not

`music-recall-memory.md` rejected trigger-based template entities because
"trigger-based templates are YAML-only, and `/config` is not writable from MCP
without HACS." **That is no longer true.** `sync_config.py` deploys `config/`
over the Samba mount, so YAML template entities — which can hold lists of
*dicts* rather than flat strings — are now buildable. They were still not
chosen, because enabling them needs a new `template:` key in
`configuration.yaml` and therefore an **HA restart**, and flat strings turned
out to be enough. Recorded because the next person will otherwise re-derive a
constraint that has expired.

**The read-path constraint has not changed and remains binding:** the agent
prompt is a Jinja template and templates cannot call services, so the store
must be template-readable. `input_select` still is.

**Measured before building on it, not assumed:** an `input_select` option
round-tripped a 150-character entry containing quotes, pipes and a date
intact. The recall list only ever held short titles, so this was not
evidence the previous work had already produced.

## Why it is injected, not a tool

The obvious design is a `recall_memory` tool the model calls when it thinks it
needs context. It is cheaper and has no size limit. **It was rejected on the
strength of defect 3b.**

That defect took nine attempts. Eight of them failed because the model
**ignored an optional signal** — a per-candidate tag, that tag moved to the
front of the list, an explicit instruction to escalate, and a stronger model
tier all changed nothing. It was only fixed when the script stopped offering
a hint and started deciding (`voice-and-music.md`, "Defect 3b").

A tool-gated memory has exactly that shape, and it fails **silently**: the
memory simply does not get consulted, and nothing in the reply says so. An
always-injected block cannot be ignored in that way. **This is the clearest
case in this project of one subsystem's negative result deciding another
subsystem's architecture** — and it is why the cap matters, since the cost is
now unconditional.

## The cost, measured

Every token here is paid on **every utterance in the house**, so the budget is
the design.

| | Tokens |
| --- | --- |
| Rules (fixed overhead) | ~190 |
| 12 entries at ~130 chars | ~390 |
| **Worst case added** | **~580, about +26% on tier-1 instructions** |

The entries alone land on the ~400-token budget that was set; the rules are
overhead on top, and that was accepted knowingly rather than discovered
afterwards. Prompt caching (`prompt-caching.md`) bills cache reads at a
fraction of fresh input, so the real cost is well below the token count — but
the prefix still grows, and the discipline that cut tier 1 from 1,739 to 1,142
tokens is the reason to keep the cap.

**The memory block sits near the end of the prompt, deliberately.** It is
volatile — it changes whenever anyone remembers something — and everything
above a change point stops being a cacheable prefix. Putting it last keeps the
long, stable instruction body cacheable. The recall list already breaks the
prefix mid-prompt, so this costs nothing extra; it would have cost something
if placed near the top, which is where "context" instinctively wants to go.

## Write path

One script, `remember_this`, exposed to Assist, with a `fact` field and an
optional `forget` boolean. One tool rather than two, because each exposed
script's schema is also paid on every utterance.

- **Remembering** prepends a `YYYY-MM-DD | fact` entry, drops any existing
  entry whose text matches, and caps at 12. Re-remembering the same thing
  therefore refreshes its date and moves it to the front instead of
  duplicating it.
- **Forgetting** removes entries matching the text, by case-insensitive
  substring, so a partial phrase works — "forget the thing about the mugs".
- **Forgetting something absent is reported honestly**, not silently
  swallowed: the reply says nothing was remembered like that. The script
  compares list lengths rather than trusting the filter to have done
  something.

**A forget path is not optional.** `music-recall-memory.md` records that a
wrong entry in the recall list is self-reinforcing with "no automatic
mechanism to detect or evict a bad entry". A memory you cannot correct is
worse than no memory, because it is confidently wrong forever.

## Verified

| Case | Result |
| --- | --- |
| Remember by voice | ✅ stored, dated, reply is the script's `say` |
| Recall in a **new** conversation | ✅ answered directly from the injected block, no tool call |
| Re-remember the same fact | ✅ replaced rather than duplicated; count unchanged |
| Forget by voice, partial wording | ✅ removed; the model passed the full remembered text from a partial mention |
| Forget something never stored | ✅ "I do not have anything remembered like that", count unchanged |
| **Facts are not instructions** | ✅ "it's dinner time", with a dinner-music preference stored, **offered** rather than starting playback |
| **Over-anchoring** | ✅ "play some jazz in the dining room", with a Living-Room preference stored, played in the **dining room** as asked |

The last two are the ones that decide whether this is a net gain. A memory
that acts on its own, or that bends unrelated requests, is worse than none —
the same failure `music-recall-memory.md` calls out as "the thing most likely
to make this a net loss".

## Found while testing: the say-equality rule is not universal

`voice-and-music.md` records that every music script's `say` field is relayed
word for word, verified byte-for-byte. **The first new script to carry a `say`
broke it.** `remember_this` returned `Forgotten.` and the assistant said
*"Done, I've forgotten that."* — confirmed against the execution trace, not
inferred from the wording.

Benign in this instance: the two sentences carry the same fact, and a forget
either happened or did not, so there is nothing for a paraphrase to get wrong.
It matters as **evidence about the mechanism**. The say rule is prompt
adherence, not structure, and it held across every music case only because
those were tested repeatedly — not because the model is obliged to obey. It
reinforces the conclusion already recorded there: only a wrapper conversation
agent, which *substitutes* the script's text rather than asking for it, makes
this structural.

## Deliberately not done

- **Automatic capture.** Rejected at scoping. There is no clean post-turn hook,
  and a store that fills itself makes pruning the real problem — with a
  12-entry cap, noise would evict the facts that matter.
- **Conversation history.** Largest context gain, largest token volume, and
  the fastest-decaying value. The cap makes it a bad trade.
- **Routines** ("lights to 40% at 7pm"). These belong in automations, which do
  them deterministically, rather than in a prompt that asks a model to infer
  them.

## Known limits

- **Twelve entries, then the oldest falls off silently.** Nothing warns that a
  fact was evicted. Re-remembering refreshes position, so what is used stays,
  but this is a real edge.
- **The store lives in `core.restore_state`, not in a config file.** Same
  property as the recall list: `input_select.set_options` never writes back to
  `.storage/input_select`, so an unclean shutdown can lose recent entries, and
  `export_ha.py` captures the helper's *definition* only. Memory contents are
  not backed up by this repo.
- **No per-person memory.** The voice satellite does not identify speakers, so
  this is one shared household list.

## How to audit this

- **What is currently remembered** — read the `options` attribute of the
  memory `input_select`.
- **Whether the block is reaching the model** — ask it something only a stored
  fact answers, in a *new* conversation, and confirm no tool fired.
- **Whether the write path is exposed** — `ha_get_entity_exposure` on
  `script.remember_this`; it must be exposed to `conversation` or nothing can
  be remembered by voice.
- **Current cost** — count the entries and multiply; see the table above.
