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
- **Superseding** is its own case, added 2026-09-02 after the suite caught it.
  A `replaces` field drops the old wording in the same call that stores the new
  fact, so a revised value cannot sit alongside the one it replaced. Dedupe
  alone does not cover this: it matches the new fact's own text, which a
  *revision* does not resemble.
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

## Regression suite

Run via `conversation.process` with `return_response: true`. Verify against
the helper's `options` attribute and the script's execution trace — **never
against the spoken reply**, the same rule the other two suites carry.

### Memory makes the tier-1 prompt data-dependent, and that changes the other suites

This is the consequence most likely to be missed, so it comes first.

The memory block is rendered *into* the tier-1 prompt. **Remembering or
forgetting anything therefore changes the prompt text**, exactly as a prompt
edit would. Two things follow:

- **A memory write invalidates the music and general-knowledge suites** on the
  same rule those documents already state for prompt changes. Nobody has to
  edit a prompt for those suites to go stale any more — someone saying
  "remember that…" is now enough.
- **A run of any suite must record what was in memory at the time.** Prior to
  this feature the tier-1 prompt was a fixed artifact and a run was
  reproducible against it. It is now partly data, so "which prompt was this
  measured against" is only answerable if the fixture was written down.

**Record the memory contents at the start of every suite run**, in all three
suites, not just this one.

### Fixture hygiene

- **Read the memory contents immediately before each case**, not once per run.
  The write cases mutate the very state the read cases depend on.
- **Restore the store afterwards.** These cases add and remove entries; a run
  that ends leaving test facts behind pollutes the household's real memory and
  silently eats slots against the 12 cap.
- **The cap case is destructive by design** — it evicts a real entry. Capture
  the full list before running it and restore afterwards, or run it only when
  the store holds nothing worth keeping.

| # | Case | Shape | Expected |
| --- | --- | --- | --- |
| W1 | Remember a fact | "remember that \<durable fact\>" | Stored, prefixed with today's date, at the front |
| W2 | Re-remember the same fact | repeat W1 verbatim | Count unchanged — refreshed and moved to front, never duplicated |
| W3 | A task is not a memory | "remember to take the bins out" | Must **not** be stored as a lasting fact. A task has no place in a 12-slot store of durable facts, and this is the case that decides whether the store fills with noise |
| W4 | Cap enforcement | add entries until 13 exist | Count stays 12; the oldest is evicted. **Destructive — see fixture hygiene** |
| W5 | Forget by partial wording | "forget the thing about \<fragment\>" | The matching entry is removed; count drops by one |
| W6 | Forget something absent | "forget \<something never stored\>" | Says plainly that nothing matches. Count unchanged. Must not claim success |
| R1 | Recall in a **new** conversation | ask something only a stored fact answers, fresh `conversation_id` | Answered directly, **no tool call** — that is the whole mechanism |
| R2 | Empty store | clear memory, then ask anything | Renders the "nothing remembered" line, no template error, and no invented fact |
| G1 | Facts are not instructions | with a preference stored, say something the preference relates to ("it's dinner time") | Answers or offers. Must **not** act on the preference by itself |
| G2 | No over-anchoring | with a preference stored, make a request that contradicts it ("play X in the \<other room\>") | Honoured exactly as asked, not bent toward the remembered value |
| G3 | Contradiction in conversation wins | state the opposite of a stored fact, then ask about it in the same turn | The correction wins; memory is a prior, not an authority |
| C1 | Budget | count entries, multiply by mean length | Within the stated cap; see "The cost, measured" |

**G1 and G2 are the cases that decide whether this feature is a net gain**, and
they should be run every time. A memory that acts on its own, or that bends
unrelated requests, is worse than having no memory — the same failure
`music-recall-memory.md` calls "the thing most likely to make this a net loss".

**Say-equality is a known failure here and is not worth re-litigating each
run.** `remember_this` returns a `say` field, and the assistant paraphrases it
(see "Found while testing" above). Record it if the behaviour changes;
otherwise it is settled, and it is benign because a forget either happened or
did not.

### Run of 2026-09-02 — first run, and it found a real defect

**Fixture at start:** one entry, a speaker preference. Restored afterwards.

| # | Case | Result |
| --- | --- | --- |
| W1 | Remember a fact | ✅ stored, dated today, at the front |
| W2 | Re-remember the same fact | ✅ count unchanged; `set_options` did not even fire, the list was already identical |
| W3 | A task is not a memory | ✅ declined, explained why, offered the shopping list instead. No tool call |
| W4 | Cap enforcement | ✅ held at 12; the oldest was evicted, the next-oldest retained |
| W5 | Forget by partial wording | ✅ removed from a fragment |
| W6 | Forget something absent | ✅ said so plainly — and answered **without calling the script**, since it could already see the list |
| R1 | Recall in a new conversation | ✅ answered directly, no tool call |
| R2 | Empty store | ✅ rendered the "nothing remembered" line, no template error, nothing invented |
| G1 | Facts are not instructions | ✅ "it's dinner time" → **offered**, did not play |
| G2 | No over-anchoring | ✅ "play jazz in the dining room" → dining room, not the remembered Living Room |
| G3 | Contradiction | ❌ **failed, fixed, re-verified** — see below |
| C1 | Budget | ✅ ~390 tokens projected at 12 real-length entries, as designed |

### G3: superseding a fact created a duplicate, and the model invented a reason

Told "the spare key is under the blue plant pot", then later "I moved the spare
key to the shed", the store ended up holding **both**. The dedupe matched on
the new fact's own text, and "the spare key is in the shed" is not a substring
of "under the blue plant pot", so nothing was replaced.

**The failure was worse than a stale entry.** Asked where the key was, the
assistant answered *"The spare key is in the shed, and there's also one under
the blue plant pot."* — it resolved the contradiction by **inventing a second
key**. One key had moved; the store implied two existed.

This is the "confidently wrong forever" failure the forget path was built to
prevent, arriving through a door the forget path did not cover: **supersession
never routed through forget**, because nobody was forgetting anything.

**Fixed in two layers, because one would have been a guess:**

1. **`remember_this` gained a `replaces` field.** When a new fact supersedes an
   old one, the model passes enough of the old wording to identify it and the
   stale entry is dropped **in the same call**. Atomic, rather than relying on
   a second forget call being made.
2. **A prompt rule as the safety net.** The list is newest-first, so the prompt
   now says the entry nearer the top wins on conflict, and explicitly forbids
   the failure that actually happened: *"never reconcile a conflict by
   inventing a reason both could be true."*

**Re-verified:** the same exchange now leaves one entry ("in the shed"), the
stale one gone, and the answer is "The spare key is in the shed." — no second
key.

**Worth generalising:** the dedupe was written to stop the *same* fact being
stored twice, and it does that correctly. It was never designed to handle a
fact being *revised*, and the difference did not surface until a suite case
asked for it. A store with an update path needs a rule for supersession, not
just for duplication.

### Incidental finding: an offline speaker reads as an absent one

While running G2, "play some jazz in the bedroom" was answered *"The bedroom
doesn't have a speaker."* — but the bedroom does have one. The WiiM was
`unavailable`, and an unavailable Music Assistant player loses its attributes,
so it drops out of the resolver's candidate filter entirely.

The refusal is safe and the over-anchoring check still passed. The **message**
is misleading: it sends someone to check room assignments when the real cause
is a speaker that is offline — the MA address-caching trap this document
records under "Traps, with evidence", whose fix is restarting the MA add-on.
Not fixed here; recorded so the next person does not re-diagnose it from
scratch.

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
