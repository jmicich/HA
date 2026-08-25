# Voice & Music — design decisions

Status as of 2026-08-20. Covers the Assist voice stack, `script.search_music`
+ `script.play_music` + `script.clarify_music_choice` + `script.set_music_repeat`,
and Music Assistant playback. Read this before changing the pipelines, the
conversation agent's prompt, or any of the four scripts.

**This doc deliberately records no auditable state.** No entity IDs, speaker
inventory, room assignments, RINCON identifiers, config hashes, or which
agent is currently wired to which pipeline. All of that changes without
notice and has repeatedly been wrong here. See "How to audit this" for the
calls that regenerate it on demand. What is recorded is design intent,
traps, and negative results.

## The three-layer split

Getting this wrong caused most of one session's bugs. Each layer owns one
thing:

| Layer | Owns | Must not |
| --- | --- | --- |
| Prompt | Intent extraction, tool choice, field filling | Hold facts HA already knows |
| Script | Resolution, ranking, safety | Guess at user intent |
| Music Assistant | Search, playback, providers | — |

**Do not put facts in the prompt that HA already knows.** The prompt once
hardcoded which room had speakers. When hardware moved this became false,
and worse, it *contradicted* the default-target rule — so a request for one
room played in another. The model was reasoning correctly from bad premises.
The prompt now derives rooms and speakers from the media player entities it
can see.

This same principle is why this doc no longer carries a hardware table. The
failure mode is identical, one level up.

## script.search_music and script.play_music

Exposed to Assist, so the model sees both as tools. Their descriptions and
field descriptions **are** the tool schema — editing them changes model
behaviour. Treat them as code.

This used to be one script. It was split 2026-08-19 to fix a defect a single
atomic call could not fix — see "Why two scripts" below before assuming a
merge back to one call is a harmless simplification.

**`search_music`** — fields: `query` (required), `media_type` (optional),
`artist` (optional). Calls `music_assistant.search`, ranks each type's
results library → Spotify → Apple Music (and by artist match, when `artist`
is given), and returns up to 4 candidates per relevant type as
`{uri, kind, name, artist}`. Plays nothing. BBC Sounds URIs are filtered out
entirely — they return spoken-word documentaries in the tracks bucket.

**`play_music`** — fields: `uri` (required), `kind` (required — `track`,
`album`, `playlist`, or `radio`; **`artist` is not an option**, see traps),
`name` (required), `artist` (optional), `player` (optional), `radio_mode` (optional — see
"Open-ended playback" below). Plays exactly
the candidate it's given via `music_assistant.play_media`. It does no
ranking or resolution of its own — that already happened in `search_music`
and, before that, in the model's choice of candidate. Fires the
`music_played` event with `played`/`kind`/`artist`, then stops with a
response variable. See `music-recall-memory.md` for what consumes that
event.

**`clarify_music_choice`** — added 2026-08-19 as an attempted fix for defect
3b (below); **built and mechanically correct, but the model does not
reliably call it — see "The recall-boost mechanism and its escalation" for
the full negative result before assuming this tool does anything in
practice.** Fields: `question` (required), `option_a_uri`/`kind`/`name`/
`artist` and `option_b_uri`/`kind`/`name`/`artist` (the two candidates to
choose between; artist optional per option), `player` (optional). Resolves
the target player exactly like `play_music` (same fail-loud guard, same
code), then calls `assist_satellite.ask_question` on the house's one voice
satellite with the two options offered as spoken answer choices, and plays
whichever the user picked. No answer, or an unmatched answer, returns an
error response — it never guesses.

**`set_music_repeat`** — added 2026-08-20 for "play/put this on repeat" and
"stop repeating" requests. Fields: `mode` (required — `one` or `off`),
`player` (optional). Same player-resolution and fail-loud code as the
others. Calls the native `media_player.repeat_set` service directly — no
search, no play, doesn't require anything to already be playing. See
"Repeat" below for the three entry points this covers and how each is
routed.

### Why two scripts

The single-call design could rank candidates but could not *judge* them. A
title collision — several real, unrelated things sharing a name — has no
signal in Music Assistant's search response to break the tie (`favorite`,
`explicit`, provider prefix; no popularity, no relevance score). The old
script silently played whichever same-titled item happened to rank first by
provider, which was frequently the wrong one.

The fix was not a better ranking heuristic. It was giving the model the
chance to do what ranking heuristics can't: recognize which "Yesterday" is
the Beatles song. `search_music` now returns the real candidates instead of
auto-picking one, and the model chooses using its own knowledge before
calling `play_music` with the exact `uri`/`kind`/`name`/`artist` it picked.

**An upstream fix was considered and rejected — see "Deliberately not
done".** Spotify's API has a real `popularity` field; Apple Music's does
not. Model judgment covers both providers with no external dependency.

**What this verifiably fixed, and what it didn't — see Open Defects #3.**
This is not a general fix for "wrong item played." It only helps where the
model's own knowledge can disambiguate; it does not help a phonetic or
garbled-transcription mismatch, even when the correct title is sitting in
the candidate list. Treat it as narrower than it sounds.

**Latency cost, measured (n=3):** the new step is the LLM round-trip between
getting candidates and choosing one — averaged **~1.4s** across three traced
samples (range 1.2–1.5s). The search and play legs are unchanged from the
single-call design (same `music_assistant.search`/`play_media` calls either
way) — search runs in well under a second, play (the real provider network
call) in 1–2.5s. This is a small sample; see "Regression suite" for how to
keep collecting it.

### The recall-boost mechanism, and its escalation — built 2026-08-19, six consecutive negative results

Built to fix defect 3b (phonetic/garbled-recall mismatches — see Open
Defects), after the search/play split fixed 3a but had no effect on this
half. Both pieces are mechanically verified correct via direct trace
inspection; **neither changed the model's actual behaviour across six full
voice-pipeline reps.** Read this before trying to extend either piece —
the failure is well-characterized, not unexplored.

**Mechanism 1 — recall-aware tagging inside `search_music`.** A new
`recall_match` variable compares the incoming `query` against every entry
in the recall list (`music-recall-memory.md`) using a hand-built
similarity function, and any candidate whose name exactly matches that
recall entry gets `"note": "matches something recently played"` and is
sorted to the front of the *entire* candidate list (not just its own
type-group — an earlier version only re-ranked within type, which left the
correct candidate buried behind every track candidate; fixed the same day
once caught).

**The similarity function itself needed two real fixes before it worked at
all — tested empirically via `ha_eval_template`, not assumed:**
1. **Bigram (2-character n-gram) Dice similarity was tried first and is
   the wrong tool for this case.** It's order-sensitive, and "Roomers" →
   "Rumours" is a letter *transposition*, not a substring match — measured
   score 0.167, statistically indistinguishable from an unrelated word
   ("Roomers"/"Yesterday" scored 0.143). Don't reach for bigram similarity
   for phonetic-style mishearings; it's built for a different kind of
   textual closeness.
2. **Character-multiset (order-insensitive) Dice similarity works, but
   only after fixing a real bug:** Jinja's `reject('eq', ch)` removes
   *every* occurrence of a repeated character, not one — so comparing a
   string against itself undercounted whenever a letter repeated
   (`cdice('Rumours','Rumours')` returned 0.71, not 1.0, until the loop was
   rewritten to remove exactly one match per iteration). After the fix,
   self-comparison is a clean 1.0 — but the *margin* between a true match
   and a near-miss is uncomfortably thin for a threshold-based decision:
   "Rumors" (a different real track, missing one letter) scored 0.92
   against "Rumours" — nearly as high as an exact match, and higher than
   the genuine target ("Roomers" itself scored 0.71). **This is why the
   final design uses fuzzy matching only for the outer query→recall-list
   check (deciding *whether* a recall connection might exist at all,
   threshold 0.5), and exact string matching (case/whitespace-normalized)
   for the inner candidate→recall-match check** — by that second stage the
   correct title is already known verbatim, so there's no more garbling
   left to compensate for, and a fuzzy re-check there just reopens the
   near-miss risk for no benefit.

**Mechanism 2 — `clarify_music_choice`, an escalation tool.** When the
model would pick something other than a noted candidate, the tool
description instructs it to call this instead of deciding itself. Built on
`assist_satellite.ask_question` (added to HA in 2025; response shape is
`{id, sentence, slots}`, `id` empty on no match). Two things worth knowing
before touching this action:
- **HA's per-action `timeout:`/`continue_on_timeout:` keys only exist on
  `wait_for_trigger`/`wait_template`** — confirmed by a hard rejection
  (`extra keys not allowed`) when tried on a plain service-call step, not
  assumed. There is no native way to bound an arbitrary action's runtime.
- **`ask_question` has documented GitHub issues describing indefinite
  hangs** — but only in multi-call-in-one-script or repeat-loop contexts.
  A single standalone call, tested directly with nobody answering, returned
  promptly with `id: null` rather than hanging. This script only ever
  makes one such call per run, by design, specifically to stay clear of
  the reported failure mode. Not proof it can never hang — just the one
  pattern actually exercised here.

**The six reps, in order, all on the exact same case ("Roomers" → should
resolve to "Rumours" by Fleetwood Mac):**

| # | What was live at the time | Result |
| --- | --- | --- |
| 1–2 | No recall mechanism yet (baseline, pre-dates this section) | Wrong track, both times |
| 3 | Tagging built, but only re-ranked *within* type — bug, not yet caught | Same wrong track (tag never reached top of list) |
| 4 | Global re-ranking fixed; tag correctly at position 1 of 12, verified via trace | Same wrong track *again* — first hint this isn't ranking |
| 5 | `clarify_music_choice` built and exposed; tool description explicitly instructs escalating on a note conflict | Model didn't call the escalation tool at all — picked a *different* wrong candidate directly (the "Roomers" **playlist**, not the track — its first queued song, "HeadBand," is unrelated content and not a new bug in itself) |
| 6 | Same as #5, conversation agent's model switched to `anthropic/claude-sonnet-5` | Identical wrong pick to #5, ~14s judgment latency vs ~1–2s on Haiku — no accuracy gain for a real latency cost |

**Conclusion, stated precisely rather than as "the model ignores hints":**
across every mechanism tried — a per-candidate tag, moving that candidate
to the literal front of the list, an explicit tool-description instruction
to escalate on conflict, and a stronger model tier — a bare, short,
no-artist, no-type-cue query resolves to the first literal text match every
time. The signal isn't being weighed *against* anything; it doesn't appear
to be entering the decision at all for this request shape. That's a
narrower, more falsifiable claim than "prompt adherence is bad," and it's
what any future attempt at 3b needs to either explain or route around —
see Open Defects 3b for what's still untried.

**Operational hazard while testing this:** every wrong pick logs its own
(wrong) title into the recall list via the existing `music_played` event —
the self-reinforcement trap `music-recall-memory.md` already documents.
During this investigation that meant "Roomers" itself repeatedly
re-entered the recall list from the *test's own* failures, at one point
briefly outscoring "Rumours" as the query's best recall match (self-match
= 1.0) and invalidating that rep. **Read the recall list's current state
immediately before every single rep of this case, not just once before the
run** — a rep that looks like a fresh test can silently be measuring
contaminated data.

### Resolution order (inside search_music)

Explicit (when `media_type` is set):

1. track → up to 4 track candidates
2. album → up to 4 album candidates
3. playlist / radio → up to 4 playlist/radio candidates
4. artist → **playlist and album candidates only** (never artist directly —
   see traps)

Fallback (when `media_type` is unset, i.e. a genre or mood):

1. playlist, album, and track candidates together — the model picks both
   the type and the item

Within each type, when `artist` is given, candidates naming that artist are
offered first; provider order (library → Spotify → Apple Music) breaks ties
either way.

## Repeat

Added 2026-08-20, covering three entry points: naming content and asking
for it on repeat, putting whatever's already playing on repeat, and turning
repeat back off. All three verified working end to end via the real voice
pipeline, not just direct script calls.

**The capability already existed natively — this was a wiring gap, not a
missing feature.** `media_player.repeat_set` (modes `off`/`all`/`one`) is a
core HA service, and the house's MA-owned speakers confirmed support for it
(checked the `supported_features` bitmask directly — bit `262144`,
`REPEAT_SET`, is present). Before this, nothing routed a repeat request
anywhere: tested empirically, "put this song on repeat" hit no tool at all
and the model asked "which song?", because nothing told it repeat was a
concept it could act on, or that "this" means whatever's already playing.

**Routing, three ways:**
- **"Play X on repeat"** — content is named, so this stays inside the
  existing `search_music` → `play_music` flow. `play_music` gained an
  optional `repeat` boolean field; when true, it calls
  `media_player.repeat_set(mode='one')` on the target player right after
  `music_assistant.play_media` succeeds, before returning. No extra tool
  call, no extra LLM round-trip.
- **"Put this on repeat" / "stop repeating"** — no new content named, same
  shape as the native pause/resume commands, except HA has no built-in
  intent for repeat. Routes to the new `set_music_repeat` script, which
  only sets the mode — it never searches or plays, and doesn't require
  anything to already be playing.
- All three verified via player state (`repeat` attribute), not the spoken
  reply: "play X on repeat" starts the right item with `repeat: "one"` in
  one call; "put this on repeat" flips an already-playing item's mode
  without interrupting playback (position kept advancing through the
  change); "turn off repeat" clears it the same way.

**Decision, explicit:** "put this on repeat" sets the mode unconditionally,
even if nothing is currently playing on the target player — it doesn't
check player state first or refuse when idle. Chosen deliberately: harmless
either way, and matches how someone might reasonably say it just before
pressing play.

**`set_music_repeat`'s tool description needed a specific, non-obvious
correction before the model would call it at all.** The first version said
what the script does but not what it *doesn't need* — the model twice
declined to call it and asked "which song is playing?" for a tool that has
no song field and doesn't care. Fixed by stating the negative explicitly:
"You do NOT need to know the song's name... there is no field for it...
Never ask the user what song is playing before calling this." Necessary
because the model was reasoning as if all music-related actions require
identifying the content, generalizing from `search_music`/`play_music`'s
actual requirement to a tool that has no such requirement. A lesson beyond
this one script: when a new music tool doesn't need something every other
music tool needs, say so, don't assume the absence of a field is enough
signal on its own.

**Defect #4 (player hallucination, still open — see Open Defects)
reproduced immediately in the new tool, unprompted, during this feature's
own verification.** A bare "put this song on repeat" (no room named) had
the model guess `player: "Sonos"` (invalid, correctly fail-loud rejected),
then retry with `player: "Sonos 2"` — copied verbatim from the guard's own
error text — landing on a real but wrong speaker instead of omitting the
field for the documented Living Room default. Not a bug introduced by this
feature; the exact same shape already documented for `play_music`. Left as
further evidence for defect #4's open root cause, not something fixed
here — retesting with the room named explicitly worked correctly and is
the verified path.

## Open-ended playback (radio mode)

Added 2026-08-21 for "play some music" / "put something on" — the case where
someone wants music but has nothing specific in mind. **This is stage 1 of a
larger "DJ mode" idea; stage 2 (LLM-curated tracklists) is deliberately not
built — see "Deliberately not done".**

**The capability already existed natively — this was a wiring gap, not a
missing feature**, exactly like Repeat above. `music_assistant.play_media`
has always accepted a `radio_mode` boolean, which makes Music Assistant keep
the queue topped up with similar tracks from the provider's similar-tracks
API after the seed item finishes. Verified by reading the live service
schema, not assumed. `script.play_music` simply never passed it.

**Design decision, and the reason it is worth stating:** the obvious build
for "DJ mode" is to have the LLM generate a tracklist. That is strictly
more expensive and strictly more exposed to defect 3c — every generated
title is an independent chance to fuzzy-match onto unrelated real content,
unattended, over dozens of tracks. MA's radio fill costs **zero marginal
LLM tokens** and cannot hallucinate a track, because it never names one.
Reach for model curation only where similar-tracks genuinely cannot express
the request, not as the default.

**`radio_mode` loses to `repeat`, structurally rather than by convention.**
The two are contradictory: `repeat: one` loops the current track forever, so
radio-filled tracks queue up behind it and never play — a silently broken
state rather than a loud one. The script computes
`radio = radio_mode and not repeat`, so the conflict is resolved before it
reaches MA rather than being left to the model to avoid. Verified: with both
flags set, `repeat_mode` is `one`, the queue holds 1 item, and the response
reports `radio: false`.

**Measured, by reading the queue rather than the spoken reply:**

| Case | Queue after | Result |
| --- | --- | --- |
| Track seed, `radio_mode` omitted | `items: 1`, `next_item: null` | Unchanged from before — backward compatible |
| Track seed, `radio_mode: true` | `items: 6`, next is a different artist | Radio fill working |
| Track seed, both flags true | `items: 1`, `repeat_mode: one` | Radio correctly suppressed |
| "play some music" via the real pipeline | `items: 38` | Model set the flag unprompted |

**Pipeline run of 2026-08-21, 4 reps, verified by trace rather than reply.**
Each rep was checked by reading `play_music`'s trace for the `radio_mode`
the model actually passed, not by what it said out loud.

| Rep | Utterance | Result |
| --- | --- | --- |
| 1 | "play some music" (from idle) | ✅ playlist seed, `radio_mode: true` |
| 2 | "put something on" (**while already playing**) | ❌ called **no music tool at all** — see below |
| 3 | "put something on" (from idle) | ✅ playlist seed, `radio_mode: true` |
| 4 | "play something like the Beatles" | ✅ `radio_mode: true` |
| 5 | "play the album Rumours by Fleetwood Mac" | ✅ `radio_mode` **not** passed, `radio: false` |

**The one failure is a real, reproducible-shaped gap, not noise.** With music
already playing, "put something on" fired neither `search_music` nor
`play_music` — trace-confirmed, both scripts' `last_triggered` unchanged —
so the model routed it to a built-in transport intent instead. The same
utterance from idle worked. **The prompt's own ordering explains it:** the
rule above the new one says bare transport controls apply when the request
"names no content at all", and "put something on" names no content. The two
rules overlap, and with music already playing the resume reading wins.

Not fixed here, and worth stating precisely rather than as "the model
ignored the rule": this is a rule-precedence collision inside the prompt,
which is a different failure from defect #3b's "auxiliary signal never
enters the decision". The obvious fix — an explicit carve-out saying an
open-ended *request to start something* is never a resume — is untried, and
per the "Repeat, no target" lesson above, the phrase list is the load-bearing
part, so extend it rather than reasoning about it abstractly. **Rep 5 is the
guard against over-correcting**: whatever is added must not start making
specific album requests set `radio_mode`.

**`radio` was added to the script's `result`, not just passed through.** Same
reasoning as the `player` grounding fix above: the model cannot honestly say
"it'll keep going" unless the tool tells it that it will. A prompt line
alone would have nothing true to ground on.

**This does not flood the recall list, and that is a property of where the
filling happens rather than a guard that was built.** MA extends its own
queue server-side; `script.play_music` fires exactly once, for the seed. So
`music_played` fires once and the recall list gains one entry, not forty.
**Do not assume this survives into stage 2** — an LLM-curated design that
enqueues each track through `play_music` would log every one, evicting the
household's real history from a 40-entry list. That is a decision stage 2
has to make explicitly, not inherit.

**What this deliberately does not do:** it cannot be steered mid-session
("less mellow", "no Motown"), it has no notion of a session or a duration,
and it cannot express a request that provider similar-tracks cannot
represent. Those are the things stage 2 would add, and they are the things
that cost real tokens.


## DJ sessions (curated open-ended listening)

Added 2026-08-21. **Stage 2 of the DJ-mode idea; stage 1 is "Open-ended
playback (radio mode)" above and is a different, cheaper thing.** Radio mode
seeds MA's similar-tracks fill and costs nothing per track. A DJ session
generates an actual tracklist against a brief, verifies every track exists
before playing it, and can be steered mid-session.

### Shape

Three tools are exposed to Assist — `start_dj_session`, `steer_dj_session`,
`stop_dj_session` — plus one internal script, `script.dj_queue_tracks`,
which is **deliberately not exposed** (verified: `conversation` exposure set
to false). All the expensive logic lives in the worker; the exposed tools are
thin. That keeps three tool schemas on the tier-1 prefix instead of four.

Session state lives in helpers because a script cannot hold state between
calls: `input_boolean.dj_session_active`, `input_text.dj_session_player`,
`input_text.dj_session_brief`, `timer.dj_session`, and
`input_select.dj_session_history`.

**The player is resolved once, at session start, and stored.** This is the
defect #4 mitigation: a session lasts an hour, and re-resolving per batch
would give the player-substitution bug an hour of chances instead of one.

### The curation engine is OpenAI's AI Task, and that was forced

`ai_task.generate_data` takes a `structure` schema and returns typed data, so
there is no prose to parse — a real advantage over the
`conversation.process` escalation pattern that `ask_general_knowledge` uses,
which returns a speech string. Nested objects work, so the model returns
`[{artist, title}, ...]` directly rather than a delimited string that would
have to be split on a separator that can occur inside titles.

**The Anthropic AI Task entity cannot do this today, and the failure is
upstream, not local.** Calling `ai_task.generate_data` with any `structure`
against the Anthropic entity fails with:

```
anthropic.BadRequestError: 400 - output_config.format.schema:
For 'object' type, 'additionalProperties' must be explicitly set to false
```

HA's `anthropic` integration builds a JSON schema from the `structure`
selector and never sets `additionalProperties: false`, which the Anthropic
structured-output API requires. The script then dies a second death with
`Last content in chat log is not an AssistantContent`. The identical call
against `ai_task.openai_ai_task` succeeds, which is what isolates it to the
Anthropic integration rather than to the schema.

**Consequence worth knowing:** curation runs on OpenAI while the rest of the
voice stack runs on Anthropic, so it does not benefit from the prompt caching
in `prompt-caching.md`, and it is a second provider to keep an eye on.
Revisit if the upstream schema bug is fixed — Anthropic is the better
music-knowledge bet and already has caching configured.

### Verification is the load-bearing part

Every generated track is searched, and a candidate is accepted only if the
normalised title matches (equal, or the candidate starts with what was asked
— so `Dreams (2004 Remaster)` passes) **and** the requested artist appears in
the candidate's artists. Normalisation strips punctuation and case, which is
required: MA returns `Paint It, Black` for `Paint It Black`, and strict
equality would drop a correct track over a comma.

**It catches real hallucinations, not hypothetical ones.** Measured drops:

| Brief | Generated | Queued | Notable drop |
| --- | --- | --- | --- |
| 70s soul and funk, upbeat | 12 | 10 | `Con Funk Shun - Love Train` — *Love Train* is by The O'Jays. A real title welded to the wrong artist, which is exactly the failure this exists to stop. |
| mellow jazz, nothing with vocals | 12 | 10 | `Billie Holiday - Solitude (Instrumental)` and `Stan Getz - The Girl from Ipanema (Instrumental)` — the model invented "(Instrumental)" variants to satisfy the brief. An instrumental Billie Holiday track is a contradiction. |

**Survival rate runs 10–12 of 12**, so strict matching does not starve the
queue — that was the open question when this was designed, and it is
answered. A `(Instrumental)`/`(Remastered)` prohibition was added to the
generation instructions after the second run, which lifted the next run to
11 of 12.

**The drop list is returned, not swallowed.** `dropped` and `dropped_titles`
come back in the result so a bad brief is visible rather than silently
producing a three-track session.

### Latency: measured, and the first fix was the whole feature

The first working version took **39.3s** before any sound: `ai_task` 12.2s,
the verify loop 23.1s (12 searches at ~1.9s each including a 250 ms spacing
delay), `play_media` 4.0s. That is not a voice feature — Assist can time out
before the music starts.

**Fix: play the first verified track immediately, then queue the rest behind
it.** Time to first sound dropped to **3.1s**, with the remaining nine tracks
landing at +26s while the first one plays. Same total work, but the room is
not silent for it.

**`radio_mode` is deliberately false on that first call and true on the
tail call.** With it true on the first call, MA immediately fills ~29
similar tracks and the curated `add` lands *behind* them — the curation would
be inaudible for two hours. Radio fill is armed only once the curated tracks
are already in the queue, where it does its intended job as the
end-of-session backstop.

**One provider call carries the whole batch.** `music_assistant.play_media`
accepts a *list* of `media_id` values, verified directly — so a 10-track
batch is two calls, not ten, which matters given the rate limiter this
document warns about elsewhere. **MA does not preserve the list order**
(measured: passing `[Stairway, Paint It Black, Dreams]` played Dreams first,
with shuffle off). Curation here is about *what* plays, not the running
order; do not promise a running order.

### Dedupe, and the duplicate that proved it was needed

The first steer queued **the track that was already playing** as the next
track. Nothing told the model what the session had already used, so
regenerating against a similar brief re-picked the same canonical songs.

Fixed with `input_select.dj_session_history`, using the same mechanism and for
the same reason as the recall list in `music-recall-memory.md`: the store has
to be template-readable, and `input_select.set_options` is the one thing that
holds an arbitrary runtime list and can be read from a template. Every
generated title goes in; the generation instructions inject the list as a
do-not-repeat set. Cleared at session start, so a *new* session may replay
favourites, and accumulated across steers within a session, which is where
the repetition actually hurt.

Verified: the steered batch after the fix had **zero overlap** with the batch
before it.

### Steering: what works, and the two things that did not

**What works.** Direction is honoured (`more upbeat, add some piano trio
swing` produced Night Train, Spain, Poinciana, Caravan), dedupe holds, the
currently-playing track is preserved, and the up-next run is replaced —
queue measured dropping 39 → 13, i.e. the old tail really is cleared.

**Getting there ruled out two approaches, both measured:**

- **`enqueue: replace_next` with `radio_mode: true` does not replace
  anything useful.** The queue stayed at 39 items and `next_item` was still
  a track from the previous batch. The radio fill armed by the previous call
  sits between the current track and the insert point.
- **`enqueue: next` is worse.** The queue ballooned 39 → 76, because each
  steer re-armed radio fill on top of the last one, and `next_item` was still
  from the batch before.

**`replace_next` with `radio_mode: false` is the combination that works.**
The rule generalises: **arm radio fill exactly once per session, on the
starting call.** Re-arming it on every steer compounds the queue.

### The open defect: a steer cannot replace the brief, only extend it

The brief accumulates (`"classic soul and Motown, upbeat; switch to 90s hip
hop instead"`), so a *replacement* instruction blends instead of overriding.
Measured: that brief produced **8 soul tracks and 4 hip hop tracks**.

**A prompt fix was tried and did not work.** Adding *"Later instructions
always win over earlier ones... drop the earlier direction completely rather
than blending"* to the generation instructions produced the identical 8/4
split. One attempt, not six — but it failed, and it is recorded so the next
person does not spend the attempt again.

**Routed around at the tier-1 layer instead, and this is the better fix
anyway.** A request that replaces the direction outright is not a steer, it
is a *new session*: the prompt now sends "instead", "actually make it" and
"switch to" to `start_dj_session` with a fresh brief. Verified end to end —
"actually switch to 90s hip hop instead" produced a brief of exactly
`90s hip hop`, a history of twelve hip hop tracks with no soul at all, and
2Pac playing. **The underlying blend inside `steer_dj_session` is still
there**; it is simply no longer reachable by the phrasings that trigger it.
A steer that *partially* replaces ("keep it upbeat but drop the Motown") is
untested and is the case most likely to still blend.

### Cost

Each generation is one `ai_task` call. A session costs one at start, plus one
per steer — roughly 1–3 an hour of listening, not one per track. Radio fill
after the curated batch is free. Against that, the three tool schemas are on
the tier-1 prefix and are therefore paid on **every utterance in the house**,
cached (see `prompt-caching.md`) but not free — the prompt grew ~830
characters and the tool schemas rather more. If DJ mode goes unused,
un-exposing the three scripts is the lever, exactly as it was for
`clarify_music_choice`.

### Room targeting is only as good as one-speaker-per-room

Added 2026-08-24, immediately after the defect #4 fix above made room names
the **preferred** way to target a speaker. That fix has a precondition it did
not state, and the precondition was not true.

**The area fallback returns the first matching player it iterates, not the
best one.** With one music player per room that is unambiguous. The Living
Room had three: the Living Room Speaker, the WiiM, and the Home Assistant
Voice puck's Music Assistant player. `states.media_player` iteration order is
not stable across reloads, so which one "living room" resolved to changed
between two measurements hours apart on the same day:

| When | `resolve('living room')` |
| --- | --- |
| During the defect #4 fix | `media_player.living_room` — correct, 3 of 3 |
| Later the same day | `media_player.home_assistant_voice_0a9a69` — **the voice puck** |

Nothing changed in config between those. The WiiM came back from
`unavailable`, the candidate list re-ordered, and a passing case silently
became a failing one. **The defect #4 suite result was therefore luck as much
as fix** — worth knowing before trusting a green run on this path.

**Note where the fault is not:** the puck is *not* exposed to the
conversation agent, so the model never asked for it. The model correctly
passed "living room". The script chose the puck. This is a resolver bug, not
a model bug — the opposite of every previous speaker defect here.

**Fix, in two parts:**

1. **One music speaker per room.** The WiiM moved to the Bedroom (it had
   physically moved; the registry still said Living Room). Rooms now hold
   exactly one music player each.
2. **A `no_music` label, and the resolver skips it.** Some Music Assistant
   players are not things anyone wants music on — a voice satellite is a
   mono puck for talking to. The `no_music` label marks them, and
   `play_music`, `clarify_music_choice` and `start_dj_session` all filter it
   out of both the candidate list *and* the guard's speaker list, so an
   excluded player can never be resolved to and is never offered as a retry.

Labels were chosen because they are **template-readable** (`label_entities()`
works, verified before building on it — the mistake `music-recall-memory.md`
records was designing around tooling that could not do what was assumed) and
editable in the UI, so a future speaker can be excluded without touching YAML.

**Verified after the change:**

| Request | Resolves to | Calls |
| --- | --- | --- |
| "play some jazz in the living room" | Living Room Speaker | 1, no retry |
| "play some blues in the bedroom" | WiiM Mini-F834 | 1, no retry |
| "play some jazz on the WiiM" | WiiM | 1, no retry — after the rename below |
| Guard payload, bogus name | puck absent; WiiM listed under Bedroom | — |

**The standing rule this leaves:** *room targeting assumes one music speaker
per room.* If a room ever gains a second, "play in that room" becomes
arbitrary again and the fix is to label one of them `no_music` or to make the
resolver prefer explicitly. Check this whenever a speaker is added or moved —
it is now part of the speaker audit.

### The alias problem has a one-line cure: make the name the alias

Aliases are unresolvable from templates and always will be. But nothing
forces a speaker to *have* aliases. The WiiM was named `WiiM Mini-F834` with
aliases `WiiM` / `the WiiM` / `WiiM Mini` — so every string the model was
shown was one the script could not resolve, and every request paid a guard
round trip.

**Renamed the entity's primary name to `WiiM` and cleared its aliases.** One
registry edit, no code. Now the only string in play is one that resolves
directly.

| Value passed as `player` | Before | After |
| --- | --- | --- |
| `WiiM` | ❌ unresolved (alias) | ✅ direct name match |
| `WiiM Mini-F834` | ✅ (primary name) | ❌ — no longer its name |
| `the WiiM` | ❌ | ❌ still, exact match only |

Verified 3 of 3 through the real pipeline, each a **single** `play_music`
call with no fast-fail: "play some jazz on the WiiM" (trace confirms
`p: "WiiM"` → `matched` by direct name, area fallback never reached), "put
Fleetwood Mac on the WiiM", and "play Take Five in the bedroom".

**The general recipe, worth applying to any speaker people name out loud:**
set the entity's *primary name* to the words they actually say, and delete
the aliases. An alias is a string the model will happily hand to a script
that cannot resolve it. **The Living Room Speaker and Sonos 2 still have
aliases** (`Sonos` / `the Sonos`, `the Era` / `dining room speaker`), so
naming either of those out loud still costs a guard retry — the same edit
would fix them, and has not been done.

Note `the WiiM` still fails: matching is exact, not substring. It costs a
guard retry and lands correctly, so it is a latency cost, not a correctness
one. Observed behaviour is that the model passes the exact string it was
shown, so this rarely fires.

**Not captured by `export_ha.py`.** Area assignments, labels and the entity
registry are not part of the exported `.storage` subset, so this change is
reproducible only from this document, not from `ha_export/`. Re-deriving it
after a restore means three registry edits: the WiiM in the Bedroom, named
`WiiM` with no aliases, and `no_music` on the voice puck's MA player.

## Traps, with evidence

**Artist-type playback triggers unbounded enumeration.** MA fans out API
calls to fetch all tracks for an artist, tripping the provider rate limiter
with repeating backoff. Playback never starts and the player falls to idle.

**This is true of library artists too.** Getting it wrong twice cost real
time. A library artist aggregates *provider mappings*, so enumerating one
still hits the streaming provider. There is no safe artist-type playback —
treat artist as last resort regardless of provider prefix. Playlists and
albums are bounded fetches.

**As of the search/play split, this is no longer just a convention.**
`play_music`'s `kind` field selector only offers `track`, `album`,
`playlist`, `radio` — `artist` is not a value the schema accepts, so
`search_music` never offers an artist candidate as directly playable either.
Direct artist playback went from "avoided by resolution order" to
"impossible to request through this tool." Keep it that way if either
script is touched again.

**Spotify before Apple Music.** Apple's limiter fired repeatedly across a
session; Spotify's once. Measured: one request went from ~3 minutes late to
immediate after the swap.

**Library items still resolve through the streaming provider.** Promoting
one provider in the *search* ranking does not govern how MA resolves
*library* playback. That is an MA-side provider-priority setting, not
reachable from HA.

**Enum stringification.** MA search results contain Python enums. Passing
raw result objects through a template variable makes HA fall back to
`str()`, so indexing a URI returns a *character* and resolves to empty — MA
then plays something arbitrary. **Always `map(attribute='uri')` to plain
strings first.** This has bitten twice.

**`variables:` blocks render one key at a time.** A key reading a name
declared lower in the same block gets an undefined value, and comparisons
against undefined fail *silently* — producing an empty target and the error
`Template rendered invalid entity IDs`. Keep such expressions
self-contained.

**Friendly names vs entity IDs.** The model passes display names where the
entity selector wants entity IDs. The script resolves both — but not
aliases; see below.

**Script templates cannot read entity aliases at all.** Confirmed by direct
template evaluation, not inferred: `entity_attr(entity_id, 'aliases')` does
not exist as a template function in this HA version, and `aliases` is not a
state attribute. So when the model passes a `player` value that matches an
alias it correctly derived, friendly-name/entity-id matching alone can never
resolve it — the previous trap's fix doesn't cover this case. **Area name is
the real fix surface**, because `area_name()` *is* a native, registry-backed
template function reachable from script logic. `script.play_music` falls
back to matching `player` against the area name of any MA-owned speaker
(case-insensitive substring) when direct entity-id/name matching fails. This
still does not cover a free-form alias that isn't a room reference (a
speaker nickname, say) — that class stays unreachable from templates by
platform limitation, not a bug in this script. If a speaker's area
assignment is missing or wrong, this fix surface silently does nothing —
audit area assignment (see "How to audit this") before assuming the script
is broken.

**Rapid testing poisons its own results.** Back-to-back playback calls trip
the provider limiter, after which every later test fails for unrelated
reasons. Space playback tests ~30s apart.

**MA caches device addresses independently of HA.** When a speaker's IP
drifts, MA keeps hammering the old address and the player goes unavailable —
while the device's *native* integration shows it healthy at the new address.
Reloading the HA config entry does **not** fix this; it only reconnects HA
to the MA server. **Restart the MA add-on.** Diagnosed once by finding
connection-refused errors in the add-on log against an address the working
integration was not using.

**HA caches device names at discovery.** Renaming a speaker's room in the
vendor app does not propagate. HA keeps the discovery-time name
indefinitely, which reads exactly like "the user never named it." **Reload
the config entry** and the name refreshes. A diagnosis of "you haven't set
the room name" was wrong for this reason — check before sending anyone to a
vendor app.

**Duplicate entities per physical speaker.** A speaker owned by both Music
Assistant and its native integration produces two media players with the
same underlying identifier. If both are exposed to Assist, the agent sees
one speaker twice — directly undermining the room/speaker derivation the
prompt depends on. **Convention: MA's entity is the exposed one; the native
twin is hidden and unexposed.** Verify this holds whenever a speaker is
added.

**Aliases go stale across moves.** An alias naming a room follows the
entity, not the room. Moving a speaker without updating its aliases lets a
request for one room target a device in another — worse than having no alias.

**A field the schema doesn't have is information the script can never see,
no matter how well the model understood the request.** `script.play_music`
originally had no `artist` field. The model correctly parsed "play So What
by Miles Davis" into `query: "So What"`, `media_type: "track"` — but had
nowhere to put "Miles Davis", so it silently vanished before resolution ever
ran. `pick_track` then ranked candidates by provider only, with no way to
prefer the right performer, and played whichever "So What" happened to rank
first. This looked at first like a model or ranking bug; it was a schema
gap. **Fix:** added an optional `artist` field, and an artist-match
preference step in `pick_track`/`pick_album` that runs *before* provider
ranking — matching candidates are tried first, and a non-matching or absent
artist falls back to the original provider-only behavior unchanged. Verified
three ways: correct artist match, unset-artist backward compatibility, and
graceful fallback on a non-matching artist all produce the expected pick.
The model populated the new field correctly on the first attempt with no
prompt changes, confirming the field description alone was the missing
piece.

**This predates the search/play split** — `pick_track`/`pick_album` no
longer exist under those names; the same artist-match-before-provider-rank
logic now lives inside `search_music`'s candidate ranking. The lesson
(a missing field is invisible to the script no matter how well the model
understood the request) still applies to both scripts equally.

**The spoken reply can invert the result, not just omit detail.** Two
"named device" suite runs had the model report failure out loud while the
correct track was verifiably already playing (checked via state, not the
reply). This is the existing "never trust the spoken reply" rule from a new
direction — the known failure mode was the model claiming success it didn't
earn; this is the model claiming failure it didn't have. Same rule, verify
state either way, but worth naming explicitly since a false failure report
could send someone chasing a bug that isn't there.

**Root cause found and partially fixed, 2026-08-20 — the scripts never told
the model which speaker they actually used.** The repeat feature's own
testing surfaced the sharpest instance yet: "put this song on repeat in the
living room" got the reply *"Repeat is on. Yesterday will loop"* while
`media_player.repeat_set` had actually landed on a different speaker
entirely, playing a different leftover song. Checked, not assumed:
`play_music`, `set_music_repeat`, and `clarify_music_choice`'s `result`
payloads carried `played`/`kind`/`repeat` but never which player was
targeted — so even a model trying to report accurately had no real data to
ground a room claim on, and could only ever be echoing what the *user*
asked for, not what the tool *did*. A prompt instruction alone cannot fix
that; there was nothing true for it to say.

**Fix: added `player` (resolved friendly name) to all three scripts'
`result`, plus a prompt instruction to ground every reported fact — song,
repeat state, and room — strictly in the tool's returned fields, explicitly
never in what the user asked for or what was called with.** Verified across
3 live reps of the exact repro shape (bare "put this song on repeat," no
room named — the phrasing already known to trigger defect #4's
player-resolution flakiness): 2/3 resolved correctly and the reply now
explicitly names the actual player from the response ("Repeat is on for
the Living Room Speaker" — it previously never named a room at all in the
success case, and named the wrong one in the failure case above). The
third rep asked which speaker rather than guessing — a safe abstention, not
a regression, but also not a reproduction of the original bug shape. **Not
fully closed:** three reps didn't happen to catch defect #4 actually
landing on a wrong player this round, so the fix is verified for the
"tool succeeded, is the reply honest about it" case, but not yet
re-confirmed for the original "tool landed on the wrong player, does the
reply admit it" case specifically — worth catching that combination
specifically next time it's tested, rather than assuming the fix covers it
by extension.

**Reopened 2026-08-24 — see the counter-example at the end of this
section. The paragraph below stands as an accurate account of the
2026-08-21 run, but the conclusion it drew does not generalise.**

**Closed 2026-08-21 — the combination landed once.** The suite run
of that date caught the exact pairing this paragraph asked for: rep 3 of
"play Fleetwood Mac in the living room" resolved to the Dining Room speaker,
and the reply said *"Now playing Fleetwood Mac Radio on the Sonos."* The
routing was wrong and **the reply was honest about it** — it named the
speaker the tool actually returned rather than echoing the room the user
asked for. Compare the pre-fix failure, where the same wrong-player
situation was reported as the *right* room. The grounding fix therefore
holds in the case it had never been observed in: it does not prevent
mis-routing, but it stops mis-routing from being reported as success.

**The counter-example, 2026-08-24.** Three reps of "play Fleetwood Mac in the
living room". Rep 1 resolved correctly. Reps 2 and 3 both played on Sonos 2 in
the Dining Room. Rep 2 reported *"Playing Fleetwood Mac Radio on the Sonos"* —
grounded, and the failure was audible. **Rep 3 reported "Playing Fleetwood Mac
Radio in the living room now"** while `play_music`'s trace shows it returned
`player: "Sonos 2"`. Same prompt, same phrasing, same session, opposite
reporting behaviour.

So the grounding instruction is **probabilistic, not structural** — which is
what this document says about every prompt-level guardrail at this model tier,
and should have been the prior. The honest claim is: grounding makes
mis-routing *usually* audible, and a listener cannot rely on hearing it. It
does not convert a silent failure into a loud one; it converts it into a
mostly-loud one.

Worth stating what that means practically: **the wrong-room bug is now
audible.** A listener hears the assistant name a room they didn't ask for,
which is a recoverable failure. That is the ceiling this architecture
allows — see the `set_conversation_response` finding below for why a
deterministic reply is not available.

**A structural fix was investigated and ruled out, confirmed empirically,
not assumed.** `set_conversation_response` — the native HA action that lets
a script dictate the exact spoken reply — has no effect on this pipeline.
Tested directly: added it to `set_music_repeat` with a distinctive marker
string, called the script through the real voice pipeline, and the actual
spoken reply was the LLM's own paraphrase ("Repeat is now off in the living
room"), not the marker text. This makes sense once traced through: our
music tools are called by an LLM *function-calling* agent
(`llm_hass_api: assist`), which always synthesizes its own final reply from
tool-result data as one more step after the tool call returns — it never
adopts a tool's response as the final utterance verbatim. `set_conversation_response`
only takes effect for HA's native/local intent-triggered conversation
flow, where the script *is* the entire response mechanism and there's no
LLM paraphrasing step to override. **No known mechanism in this stack lets
a script force its own wording into the spoken reply when called as an LLM
tool.** The prompt-level grounding fix above (return the real data, instruct
the model to use only that data) is therefore the ceiling of what a *script*
can do — **superseded 2026-08-24 by the `say` field, which is a strictly
better version of the same prompt-level idea; see "The `say` field" below,
including the custom-agent design that would make it structural** — not a
stopgap on the way to something stronger — reflect
that before promising a fully deterministic reply is possible without
changing the architecture (e.g. dropping to a local hassil-matched intent
for these specific commands, which is a materially bigger change and has
its own documented risk — see "Deliberately not done").

**Having the right answer in context does not mean the model uses it.** The
"Roomers" → "Rumours" mangled-recall case fails even when the correct title
is present *twice over* — once as a `search_music` candidate correctly
tagged with the right artist, and again by name in the recall-list prompt
hint (`music-recall-memory.md`) — and it fails the same way every time, not
intermittently. The model consistently prefers the literal text match (a
real playlist titled "Roomers") over the semantically-correct one already
handed to it. This means the fix for defect #3's title-collision half (see
"Why two scripts" above) does not generalize to its phonetic-recall half —
more context pointing at the right answer isn't sufficient when literal
text similarity points somewhere else. See Open Defects #3 for the current
split.

### The `say` field: stop asking the model to compose, ask it to repeat

Added 2026-08-24. This is the third and best attempt at making the spoken
reply match what actually happened. Read the two above it first — the
`player` grounding fix and the `set_conversation_response` dead end — because
this one only makes sense as a response to what both established.

**The mismatch is not one problem, and scoping it is what made the fix
obvious:**

| Class | Example | Tool result available? |
| --- | --- | --- |
| A. Wrong fact about a real action | tool returned "Sonos 2", reply said "living room" | ✅ truth was there, unused |
| B. False failure | reply says it failed, the track is verifiably playing | ✅ |
| C. False success, **no tool fired** | bare "pause" claimed done, nothing was called (defect #2) | ❌ **none** |
| D. Fabricated detail | added a room or a qualifier the tool never returned | partly |

**C is the one that defeats every tool-side fix**, because there is no result
to ground anything in. Keep that in mind before believing any claim that the
mismatch is "solved".

**What changed.** The previous design returned five structured fields
(`played`, `kind`, `repeat`, `radio`, `player`) and a prompt rule saying
*ground every reported fact in these*. That asks the model to **compose** a
sentence from parts — and composition is where it drifts.

Each script now also returns **`say`: one complete, pre-composed sentence**,
built from the same resolved values the script actually used. The prompt rule
became *speak the `say` field as your entire reply, word for word*. The ask
shrinks from "compose a claim" to "repeat this sentence", which is a far
smaller thing to get wrong.

```
say: "Now playing Rumours on Sonos 2."
say: "Now playing Dreams on WiiM. It will keep playing similar music."
say: "Repeat is on for Living Room Speaker."
say: "DJ stopped."  /  "No DJ session was running."
```

**Errors deliberately have no `say`.** The guard's error is an instruction to
*retry*, not something to read out. Giving it a `say` would make the model
announce "no speaker is named X" instead of trying again, converting a
self-healing path into a dead end. The prompt says: `say` present → speak it;
`say` absent or empty → follow the `error`. `start_dj_session` and
`steer_dj_session` render an empty `say` when nothing was queued, for the same
reason.

**Verified — the reply is byte-identical to the field, not merely similar:**

| Utterance | `say` in the trace | Spoken reply |
| --- | --- | --- |
| "play the album Rumours in the dining room" | `Now playing Rumours on Sonos 2.` | `Now playing Rumours on Sonos 2.` |
| "put this song on repeat" | `Repeat is on for Living Room Speaker.` | `Repeat is on for Living Room Speaker.` |
| "stop the DJ" (none running) | `No DJ session was running.` | `No DJ session was running.` |

**A wrong action became audible, which is the point.** The repeat test ran
with *two* speakers playing, so `set_music_repeat` fell back to the default
speaker rather than the one playing — and said "Living Room Speaker" out
loud, which is exactly what it did. Under the old design the reply would have
been composed around the song the user meant. The speech no longer disagrees
with the action; a questionable action is now something you can hear.

**What this does not do, stated plainly:**

- **It is still probabilistic.** The model is still generating the final
  utterance; it is merely being asked for a much easier thing. Expect it to
  hold far more often, not always.
- **Class C is untouched.** When no tool fires, there is no `say`, and
  nothing here prevents a confident "done".
- The three-item and word-cap style rules elsewhere show that prompt rules of
  this kind get rounded off under pressure. Treat a green run as evidence,
  not proof.

**The structural answer, specified but not built.** Everything above is still
prompt-mediated. Making it structural means the spoken text stops being
*generated* and starts being *selected*: a custom conversation agent
(`config/custom_components/`) that becomes the pipeline's agent, delegates to
the existing LLM agent for routing and candidate judgment, and then — if one
of our music scripts fired during the turn — **substitutes that script's
`say` for the LLM's narration** before it reaches TTS. That closes A, B, C
and D, because a claim can no longer be authored by the component that
guesses.

Two things make it more feasible than it sounds, both checked rather than
assumed: `sync_config.py` filters by a DENY list rather than an allow list,
so `config/custom_components/` deploys through the existing pipeline
unchanged; and the repo already carries the testing convention for it
(`tests/components/<domain>/`, `pytest-homeassistant-custom-component`).

The risk is proportionate and worth stating before anyone starts: such an
agent sits in the path of **every utterance in the house**, so a bug there
takes out all voice control, not just music. `CLAUDE.md`'s intended layout
also puts `custom_components/` at the top level, where HA would not load it —
that needs reconciling first.

## Regression suite

Run via `conversation.process` with `return_response: true`; verify with a
state read, not the spoken reply. **Run each case at least three times** —
several failures here are probabilistic.

Pass/fail status is deliberately not recorded: it is a measurement with a
short shelf life, and a stale one invites false confidence.

**Since the search/play split, verify via both scripts' traces, not just
one.** Pull `ha_get_automation_traces` for `search_music` and `play_music`
after every case: confirm search ran and see what it actually returned,
confirm `play_music`'s `uri` was genuinely one of those candidates (a model
could in principle invent one — checked and not observed so far, but this
is a new failure class the old single-script design couldn't have, so keep
checking it rather than assuming it stays clean), and read the final player
state.

**Every case that fires a music script also carries a say-equality check,
added 2026-08-24.** This is a *method* rule, not a single case — it applies
to every row below that succeeds, because the failure it catches (the spoken
reply disagreeing with the action) can appear on any of them.

For each successful case, capture both:

- the script's `result.say` from its execution trace, and
- `response.speech.plain.speech` from the `conversation.process` result.

**They must be equal after trimming — byte for byte, not merely consistent
or "close enough".** A reply that says the same thing in different words has
already failed, because the whole mechanism is the model repeating a
sentence rather than composing one; a paraphrase means it composed, and the
next one may compose something untrue. Record any inequality verbatim, both
strings, rather than summarising it — the shape of the drift is the finding.

**The error path is the other half and inverts the check.** A response with
no `say` (or an empty one) must NOT be spoken: the guard's error is an
instruction to retry. Passing means the model retried, or said plainly that
it did not work — never that it read the error text aloud as though it were
a confirmation.

**New failure mode from the split, distinct from the old design's:** the
old script had a hardcoded `default: no music found` branch. That branch no
longer exists — `search_music` can return an empty or useless candidate
list, and it is now entirely on the model to recognize that and decline to
call `play_music` rather than inventing a play. Watch the nonsense-query and
room-with-no-speaker cases for this specifically; it is a real thing that
can fail, not a formality inherited safely from the old design.

**Latency capture, added 2026-08-19 — cheap, no new instrumentation.** For
each case that plays something, pull both scripts' trace start/finish
timestamps and record three numbers: `search_music` duration, the gap
between `search_music` finishing and `play_music`'s first call starting
(the LLM candidate-judgment round-trip — the number that matters, since
it's the cost this design added), and `play_music` duration. Report
mean/min/max across the run rather than per-case noise; three samples
(2026-08-19) put the judgment gap at ~1.4s. Worth tracking over time as the
model or prompt changes, separately from correctness.

| Case | Shape |
| --- | --- |
| Artist | "play [artist]" |
| Song by artist | "play [song] by [artist]" |
| Album | "play the album [name]" |
| Song, no artist | "play [song]" — must be the real track, not a cover |
| Genre | "play some [genre]" |
| Playlist by name | "play my [name] playlist" |
| Room with a speaker | "play [artist] in the [room]" |
| Named device | "play [artist] on the [device]" — including a device whose primary name the model does not surface; it must call the tool and use the guard, not refuse |
| Named room, both rooms | "play X in the [room]" for **each** room that has a speaker — a fix that biases every request to one room passes the first and fails the second |
| STT variant | mangled artist name, as speech-to-text would garble it |
| Room with no speaker | must refuse, not substitute. **Use the Kitchen or the Attic** — the Bedroom gained a speaker on 2026-08-24 and is no longer empty |
| Nonsense query | must not fuzzy-match to real content |
| Pause, no target | |
| Pause, with target | |
| Resume, with target | |
| Repeat, new content | "play [song] on repeat" — must both play the right song AND set `repeat: "one"` in the same request |
| Repeat, current, no target | "put this song on repeat" / "repeat this" — no room named; this is the exact phrasing that triggered defect #4 during this feature's own verification, see "Repeat" above |
| Repeat, remove | "stop repeating" / "turn off repeat" |
| Spoken reply on an error path | force the unknown-speaker guard (name a speaker that does not exist) — the model must retry or admit failure, and must **never** speak the error text as a confirmation |
| Open-ended, from idle | "play some music" / "put something on" — must play something and set `radio_mode`, never ask what they want |
| Open-ended, while playing | same phrasing, but with music already playing — **known to fail**, routes to resume instead; see "Open-ended playback" |
| Open-ended, vibe | "play something like [artist]" — `radio_mode` true |
| Specific request stays specific | "play the album [name]" — `radio_mode` must **not** be set; this is the over-triggering case |
| DJ session, start | "be my DJ, something for cooking dinner" → `start_dj_session`, music inside ~5s, `dropped` low |
| DJ session, duration | "...for the next 45 minutes" → `timer.dj_session` active, and the session ends when it fires |
| DJ session, steer | "less mellow" mid-session → current track survives, up-next replaced, no overlap with what already played |
| DJ session, replace | "switch to [genre] instead" → must route to `start_dj_session`, NOT `steer` — see the open defect |
| DJ session, stop | "stop the DJ" → playback paused, session flag off, timer idle |
| DJ session, steer with none running | must refuse, not silently start one |
| Mangled recall | garbled version of a previously played item → maps to it |
| Novel request | absent from the recall list → treated as new, **must not** bend |

The last two exist because of the recall list; see `music-recall-memory.md`.

### Prompt slimming, 2026-08-20 — what came out and what it cost

Tier 1's prompt is paid on **every utterance in the house**, so it was cut
from ~1,739 to ~1,142 tokens. Nothing was removed for brevity: each cut was
either a rule stated in two places or a tool measured not to work.

- **`clarify_music_choice` un-exposed.** It carried ten fields — the largest
  tool schema here — and this document already recorded that the model does
  not reliably call it, across six consecutive negative results. Paying for
  it on every request bought nothing. The script itself is left in place as
  a record; un-exposing is what removes the cost, and it is reversible.
- **The searching-and-choosing rules now live only in `search_music`'s
  description.** They were previously stated there *and* in the prompt. The
  tool description is the better home: it is what the model reads at the
  moment it decides.
- **Repeat routing detail likewise lives only in `set_music_repeat`'s
  description**, which is the copy that earned its length by fixing a real
  refusal.

**The general rule this suggests:** when a rule is about *how to use one
tool*, put it in that tool's description, not the prompt. The prompt should
carry only what spans tools — routing between them, and how to report.

### "Repeat, no target" — fixed, and the diagnosis was instructive

**Status: 3 of 3, verified by reading the `repeat` attribute.** Getting there
corrected a wrong theory, so the route matters more than the result.

The case broke immediately after the prompt slimming above — from 1 of 3 to
0 of 4. Three attempts to argue the model out of it all failed: the existing
prompt rule, an explicit "do not ask which speaker" added to the prompt, and
that same instruction moved into the `player` field's own description. The
conclusion drawn at the time — *this needs a structural fix, wording will
never work* — matched this document's own standing rule and was wrong.

**The structural fix was applied and did not fix it either.** With `player`
removed entirely, so there was literally nothing to ask about, the model
simply moved its question to the other axis: it began asking *which song was
playing*, for a tool whose description states it has no song field.

What actually worked was restoring specificity the slimming had removed. The
old routing rule listed the trigger phrasings explicitly — "put this on
loop", "turn off repeat", and others. The trimmed version kept only "repeat
this" and "stop repeating", and the failing request was *"put this song on
repeat"*. Listing the phrasings again fixed it on the next attempt, 3 of 3.

Two things worth carrying forward:

- **That phrase list was load-bearing, not duplication.** It looked like
  verbose restatement of a rule already implied elsewhere. It was doing the
  work. When trimming a prompt, near-duplicate *examples* are the most
  dangerous thing to cut, because what they buy is invisible until a request
  falls outside what survives.
- **"Wording can't fix this" is a claim that needs testing, not assuming.**
  Three failures in a row made the structural explanation feel settled, and
  it was reached before the actual cause had been isolated. The structural
  change was kept regardless — `set_music_repeat` no longer takes a `player`
  and resolves the speaker from whatever is currently playing, which is both
  closer to what "repeat *this*" means and removes this tool's exposure to
  defect #4 — but it was not what fixed the bug.

### Run of 2026-08-25 — first run with the say-equality check enforced

Occasioned by the `say` field and the tier-1 prompt rule that goes with it.
Speaker inventory re-audited first: one music player per room after the
`no_music` label, Kitchen and Attic speakerless, Bedroom now holds the WiiM.

**Say-equality: every case that produced a `say` relayed it exactly.** Two
were compared byte-for-byte against the execution trace; the rest were
checked against the exact `say` template with the resolved player confirmed
from player state. Recording which is which because they are not the same
strength of evidence.

| Case | Spoken reply | Check |
| --- | --- | --- |
| "play Yesterday on the Bedroom Speaker" | `Now playing Yesterday on WiiM.` | ✅ trace byte-match |
| "play the album Rumours in the dining room" | `Now playing Rumours on Sonos 2.` | ✅ trace byte-match |
| "play Fleetwood Mac in the living room" ×3 | `Now playing Fleetwood Mac Radio on Living Room Speaker.` (rep 2 with the radio suffix) | ✅ template + state |
| "put this on repeat" / "turn off repeat" | `Repeat is on/off for WiiM.` | ✅ template + state |
| "be my DJ … for the next 30 minutes" | `Starting a DJ set on Living Room Speaker. It will run for 30 minutes.` | ✅ template, duration branch |
| "stop the DJ" | `DJ stopped.` | ✅ template |

**The error path passed, and passed by composing rather than repeating**,
which is the intended split. "Play Yesterday on the Bose" fired
`play_music`, hit the guard, touched no player, and the model replied *"I
don't have a speaker named Bose. The available speakers are … Sonos 2 in the
Dining Room, and WiiM in the Bedroom."* It did not read the guard's error
text aloud, and it used the paired room data correctly.

**Room targeting: 3 of 3** on the case that was 1 of 3 before the
`no_music` / one-speaker-per-room work — and this time it is not luck,
because the precondition is enforced rather than assumed.

Also passing: kitchen refusal, nonsense abstention, repeat both directions,
DJ start/stop.

**Completed 2026-08-25 — the cases the first pass left out.** Same method:
say-equality on every success, state or trace for the action.

| Case | Result |
| --- | --- |
| Playlist by name | ✅ "Rock Mix", correct player |
| Song, no artist | ✅ Take Five — the Brubeck original, not a cover |
| Album + room | ✅ Kind Of Blue, started at track 1, Dining Room |
| STT variant | ✅ "flitwood mack" → Fleetwood Mac Radio, self-corrected on a second search |
| Pause, no target | ✅ actually paused, position held at 15 |
| Resume, with target | ✅ resumed from 15 |
| DJ start, no duration | ✅ timer correctly left idle, `say` had no duration clause |
| DJ steer | ✅ queue truncated to 12, current track preserved at 81s, new batch behind it |
| DJ stop | ✅ |
| Mangled recall | ❌ "Roomers" played literally — **eighth consecutive negative** |

**Defect 3b is unchanged, and the `say` field changed how it fails rather
than whether.** The reply was *"Now playing Roomers on Living Room
Speaker."* — the wrong pick, stated plainly enough to notice. Previously the
reply could have been vaguer. That is the alignment work doing its job on a
case the resolution work still cannot fix.

Recall-list hygiene performed: the rep logged "Roomers" as it always does,
and it was removed afterwards with "Rumours" left intact.

### Run of 2026-08-24 — after the DJ-session work (stage 1 + stage 2)

Full re-run of both suites, occasioned by the tier-1 prompt changes for radio
mode and DJ sessions. Speaker inventory and room assignments audited first;
Kitchen, Bedroom and Attic confirmed speakerless **by design**, so the
empty-room cases are meaningful.

| Case | Reps | Result |
| --- | --- | --- |
| Room with a speaker | 3 | ❌ **1 pass, 2 wrong room** — worse than 2026-08-21's 2/1; see defect #4's root cause |
| Room with no speaker | 3 | ✅ refused all three, listed real alternatives, nothing played |
| Nonsense query | 3 | ✅ abstained all three |
| Named device ("play … on Sonos 2") | 2 | ❌ **refused a valid speaker both times** — "I don't see a speaker called Sonos 2". New case, and the clearest single symptom of the alias gap |
| Song by artist | 1 | ✅ So What / Miles Davis, correct player |
| Genre | 1 | ✅ Smooth Jazz Chill, radio armed |
| STT variant | 1 | ✅ "flitwood mack" → Fleetwood Mac Radio |
| Novel request | 1 | ✅ Hamilton cast recording, did not bend to the recall list |
| Mangled recall | 1 | ❌ "Roomers" played literally — **7th consecutive negative**, defect 3b unchanged |
| Pause, no target | 1 | ✅ actually paused, position preserved — defect #2 did **not** reproduce |
| Resume, with target | 1 | ✅ position preserved across the pair |
| Repeat, new content | 1 | ✅ Superstition + `repeat: one` in one request |
| Repeat on / off, no target | 1 each | ✅ correct player, no asking |
| Open-ended, from idle | 1 | ✅ radio armed |
| Open-ended, while playing | 1 | ✅ **previously failing case now passes** — routed to search+play instead of resume. One rep; not proof it is fixed |
| DJ start (with duration) | 1 | ✅ brief composed, player stored, timer armed |
| DJ steer | 1 | ✅ current track preserved, queue truncated to 12, new batch queued |
| DJ replace ("switch to X instead") | 1 | ✅ routed to `start`, brief clean, no blend |
| DJ stop | 1 | ✅ paused, flag off, timer idle |
| DJ steer with no session | 1 | ✅ refused, did not start one |

**Not run:** playlist-by-name, song-with-no-artist, album-by-name on the
default player, and second/third reps of every one-rep case above.

**Housekeeping performed:** the mangled-recall rep logged "Roomers" into the
recall list as it always does; it was removed afterwards and "Rumours" left
intact, so the fixture is clean for the next run.

### Run of 2026-08-21 — after the tier-1 prompt slimming and cache migration

Run against the migrated tier-1 agent. **Partial: the fragile and
defect-prone cases got the mandated three reps, the settled happy paths got
one.** Recorded honestly rather than presented as a full pass, since a
one-rep result on a probabilistic suite proves nothing — the defect below
was caught *only* on the third rep.

| Case | Reps | Result |
| --- | --- | --- |
| Room with a speaker | 3 | ❌ **2 pass, 1 wrong room** — see defect #4 |
| Room with no speaker | 3 | ✅ refused, listed real alternatives, no stray playback |
| Nonsense query | 3 | ✅ abstained, nothing played (new fixture — see 3c) |
| Repeat on, no target | 3 | ✅ three phrasings, `repeat: one`, correct player |
| Repeat off | 3 | ✅ three phrasings |
| Song, no artist | 1 | ✅ Take Five / Brubeck, defaulted to Living Room |
| Album + named device | 1 | ✅ started at track 1 of *Rumours* |
| Genre | 1 | ✅ resolved to a Smooth Jazz playlist |
| Garbled transcription | 1 | ✅ "flitwood mack" → Fleetwood Mac, self-corrected via a second search |
| Recall, descriptive | 1 | ✅ "that jazz thing I had on earlier" → Smooth Jazz Chill |
| Pause / resume, no target | 1 each | ✅ position preserved across the pair |

**Not run:** playlist-by-name, mangled recall ("Roomers" → "Rumours", known
open at 3b), novel/out-of-scope requests, and second/third reps of
everything in the one-rep block. The one-rep passes are evidence the case
is not *always* broken; they are not evidence it is reliable.

**The repeat regression from prompt slimming is genuinely fixed** — 3/3 in
both directions across three distinct phrasings ("put this on repeat", "put
this song on loop", "keep playing this over and over"), each resolving the
player without asking. That was 1/3 immediately after the slim, so the
restored phrase list is doing real work; see the slimming section above.

**Unexplained anomaly worth watching: `script.play_music` fired twice** on
three of the plays (the second firing 1–2s after the first, `mode: restart`
so the first is cancelled). Outcome was correct every time, and `restart`
mode is why. Not diagnosed — noted here so a future investigation into
duplicate queue entries or wasted `music_assistant.search` calls against the
rate limit starts with a known prior rather than treating it as new.

### This suite is invalidated by tier-1 prompt changes that look unrelated

The tier-1 agent both routes music *and* decides when to escalate a general
knowledge question (`general-knowledge-search.md`). They share one prompt,
so a change to the escalation wording invalidates this suite too. That is
not theoretical — a run triggered by exactly such a change surfaced two
things worth recording:

- **"Repeat, current, no target" degraded to asking rather than acting: 1
  of 3 reps did the right thing.** The other two asked which speaker to use
  instead of omitting `player` and taking the documented Living Room
  default. This is the same fragility as defect #4 — the model is unwilling
  to commit to a player when none is named — but expressed as a question
  rather than as the wrong guess. Asking is the safer failure, and it is
  still a failure: the prompt says act on the most likely reading. Whether
  the escalation edit made this worse was not isolated; it was already the
  case this suite flags as its most fragile.
- **The nonsense-query case failed, but the measurement was contaminated**
  and should not be counted. The nonsense word used had itself been logged
  into the recall list by an earlier failed run, so the model matched it as
  a legitimate recent play. This is the self-reinforcement trap in
  `music-recall-memory.md` biting a *test* rather than a user. **Clean the
  recall list immediately before this case, every time** — a prior run's
  failure silently becomes this run's input.

  **Partially retracted, 2026-08-21.** The hygiene rule above stands and is
  worth keeping. The specific diagnosis was wrong: the word in question
  ("Flibbertigibbet") is a real song, so it entered the recall list by
  playing correctly, not by failing. The case was mis-scored as a failure
  because the fixture was never a nonsense string in the first place — see
  Open Defects 3c. Two separate mistakes stacked here, and only the second
  was real: an unverified assumption about the fixture, then a contamination
  story invented to explain the resulting "failure". **Check that a nonsense
  fixture is actually absent from the catalogue before blaming the harness
  for matching it.**

**Repeat cases verify via the player's `repeat` attribute, not the spoken
reply** — same rule as everywhere else in this doc, and specifically
important here since the reply already misreported the target device once
during this feature's initial verification (see "Repeat" above). "Repeat,
current, no target" is deliberately the bare, no-room phrasing precisely
because that's what surfaced defect #4 in this tool; a phrasing that
already names a room will not exercise that path and isn't a substitute
for this case.

**Before running:** audit the speaker inventory and room assignments first.
Three cases depend on which rooms have speakers, and an empty room that is
empty *by accident* rather than by design makes the empty-room result
meaningless. This caught out a planned suite run once already.

**Any prompt change nominally invalidates the whole suite**, since prior
runs depended on one canonical prompt version.

## Open defects

**Model non-determinism is the dominant remaining failure mode.** Identical
input produces different tool choices between runs. Not fixable in config,
and single-run tests cannot distinguish "broken" from "unlucky".

1. **Empty-room substitution, intermittent.** A request naming a speakerless
   room correctly refuses on some runs and substitutes another room on
   others, with no config change between. Prompt-level guardrails are not
   dependable at this model tier; anything that must not happen needs to be
   structurally impossible in the script.
2. **Bare "pause" does nothing but claims success.** The model reports
   pausing while both players keep playing and no entity is touched. Naming
   a target works.
3. **No relevance threshold — split into three parts as of the search/play
   redesign, 2026-08-19.** Originally one defect ("nonsense queries
   fuzzy-match to real content"); the evidence no longer supports treating
   it as one problem.

   **3a. Title collisions where the model can judge — fixed.** Several real,
   unrelated items share a title with no signal in MA's search response to
   break the tie. `search_music`/`play_music` (see above) fixed this by
   letting the model choose from real candidates using its own knowledge
   instead of an auto-picked, unranked-by-relevance result. Verified 4/4
   across this session's reps on the case that motivated the fix ("Yesterday"
   → the Beatles, not the same-titled Lil Peep track, Daniel Leggs album,
   or Toosii album).

   **3b. Phonetic or garbled-transcription mismatches — still open, now
   backed by six consecutive negative reps, not just three.** The
   mangled-recall case ("Roomers" → should resolve to "Rumours") has never
   once resolved correctly across every mechanism tried: recall-list
   prompt hint alone, a per-candidate structural tag, that tag moved to the
   literal front of the full candidate list, an explicit tool-description
   instruction to escalate to a clarifying question on conflict, and a
   stronger model tier (Sonnet 5, reverted — no accuracy gain, ~10x the
   judgment latency). Full mechanism detail, the two fixed bugs in the
   similarity function, and the rep-by-rep table are in "The recall-boost
   mechanism, and its escalation" above — read that before attempting
   another fix here, since it rules out several approaches that look
   promising but have already been tried. **Precise characterization:** a
   bare, short, no-artist, no-type-cue query resolves to the first literal
   text match regardless of what other signal is available or how
   prominently it's presented — the auxiliary signal doesn't appear to
   enter the decision at all for this request shape, rather than being
   weighed and losing. Untried: routing the recall check entirely outside
   the model's judgment (deterministic query rewrite before search, which
   was the original idea but was deliberately tempered down to a safer
   tag-only approach after a false-positive risk was found in the
   similarity function — revisit the rewrite approach now that the safer
   one has been shown not to work either); a different model provider
   entirely, not just a different Anthropic tier; or accepting this as a
   permanent limitation of prompt-mediated resolution and building a
   deterministic override specifically for exact recall-list matches that
   bypasses model judgment entirely for this one case.

   **3c. Nonsense queries fuzzy-matching to real content — still open,
   still probabilistic.** Unchanged by the redesign: passed cleanly with a
   random test phrase, failed when a test phrase coincidentally overlapped
   real content. Search still essentially always returns *something*, and
   nothing structurally prevents the model from treating a low-quality match
   as good enough. The original proposed mitigation (script returns the
   resolved item's name so the model reports what actually played, rather
   than parroting the query) is superseded by the redesign — `play_music`
   already does this via its `name` field — but does not fix the underlying
   over-matching; it only makes a bad match visible in the reply instead of
   silently confirmed.

   **Passed 3/3 on 2026-08-21 — and the run invalidated the old test
   fixture.** "Flibbertigibbet", used previously as the nonsense string, is
   **a real song** — by Juli Lee, on the album *Overrated*. Searching it
   returns a genuine match, and playing it is correct behaviour, not
   over-matching. An earlier session had recorded its appearance in the
   recall list as contamination and "cleaned" it; that cleanup was removing
   a correct entry. The lesson is the ordinary one for this project: the
   assumption that a funny-sounding word is not a real title was never
   checked against the catalogue.

   Replaced with `Zqxjvwm Blarghenshpiel`, which is orthographically
   implausible in English rather than merely unusual. Three reps all
   abstained cleanly — searched, rejected the returned candidates, played
   nothing ("I couldn't find anything matching... That doesn't appear to be
   a real artist or song"). **This does not close 3c**: an abstention on a
   string with no plausible near-match is the easy half. The open half is a
   nonsense string that *does* overlap real content, which is the case that
   failed before and was not re-tested here.

**Earlier fix that caused a regression:** adding `media_type` initially
routed artist intent straight to a streaming artist URI, reintroducing the
rate-limit hang. Artist intent must resolve via playlist/album.

4. **Wrong speaker for a named room — FIXED 2026-08-24.** Recorded until now
   as "the model hallucinates a `player` value that doesn't exist",
   even when the user named no room or device at all. Confirmed via trace
   evidence: on one query, two consecutive attempts passed plausible-but-
   nonexistent entity-id-shaped strings, both correctly refused by the
   "unknown speaker" guard; the model then copied a name verbatim out of
   that guard's own `valid_speaker_names` error text on a third attempt and
   succeeded — but landed on a different speaker than the documented default
   ("omit to use the Living Room Speaker"), since it happened to match by
   friendly name rather than by omission. The refuse-on-unknown-name guard
   is doing its job here; the gap is upstream, in why the model invents a
   target at all — **still open, not fixed by the mitigation below.**

   **Cost mitigated, 2026-08-19:** the sequence order was: search, then
   resolve+validate player, then fail loudly. Every hallucinated attempt was
   therefore paying for a real `music_assistant.search` call before the
   validation that was always going to reject it — eating into the
   rate-limit budget on every wasted guess. Reordered to validate `player`
   *before* calling search. Verified against a live, naturally-occurring
   instance of this exact defect (not a synthetic test): two hallucinated
   attempts each completed in ~2ms with the trace showing the search action
   never reached, versus ~1.2s for the successful third attempt that
   actually called it. Confirmed no regression: a valid `player` value and
   an omitted one both still resolve exactly as before.

   **Reproduced again, harmlessly, inside the search/play redesign's own
   verification run:** the model guessed a plausible-but-wrong speaker name,
   `play_music` fast-failed in ~2ms with the valid-names list, and the model
   retried and succeeded with the correct name — keeping the same correct
   `search_music` candidate across the retry. The split didn't change this
   defect's shape or its mitigation; the fail-loud guard lives entirely in
   `play_music`, unchanged.

   **Reproduced a third time, same shape, in `set_music_repeat`'s own
   verification (2026-08-20)** — see "Repeat" above. Confirms this is a
   property of the model's general player-resolution behavior, not
   something specific to `play_music`; any new script reusing the same
   resolve-player code should expect the same failure mode and the same
   fail-loud mitigation, not a fresh one.

   **A fourth reproduction, 2026-08-21, and it is a different shape that
   the existing mitigation cannot catch.** "Play Fleetwood Mac in the living
   room", run three times identically: reps 1 and 2 played on the Living
   Room Speaker; rep 3 played on **Sonos 2, which is in the Dining Room**.

   Every previous instance of this defect involved the model inventing a
   speaker name that *does not exist*, which the unknown-speaker guard
   refuses in ~2ms. This one is categorically different: **"Sonos 2" is a
   real, valid, correctly-named speaker.** The guard validates that a name
   resolves to a real player; it has no notion of whether that player is in
   the room the user named. So the request passed validation and played in
   the wrong room, and no amount of hardening the fail-loud guard would
   have stopped it — the guard was working exactly as designed.

   **This moves the defect from "model invents targets" to "model
   substitutes a valid wrong target for an explicitly named room", which is
   the more serious form** — it is silent at the validation layer, and the
   only thing that surfaces it is the grounded reply naming the speaker
   (see "Repeat" above, where this same run closed that question).

   **Probabilistic at roughly 1-in-3 on this phrasing.** A single rep shows
   green; this is precisely why the suite mandates three. Do not conclude
   from one clean run that it is fixed.

   **ROOT CAUSE FOUND, 2026-08-24. This defect is not the model "inventing" a
   target. The model is passing exactly the name it was shown; the name it was
   shown is not a name the script can resolve.**

   Asked directly to list the speakers it can see, with tools disabled, tier 1
   answers with **aliases, not primary names**:

   | What the model sees | Room it reports | What it actually is |
   | --- | --- | --- |
   | "Sonos" | Living Room | alias of the Living Room Speaker |
   | "Dining room speaker" / "the Era" / "Era 100" | Dining Room | alias of **Sonos 2** |
   | "WiiM" / "WiiM Mini" | Living Room | alias of the WiiM |
   | Justin's 3rd TV | none | its real name (it has no aliases) |

   **The primary names never reach the model at all.** `play_music`'s `player`
   field description says it accepts "ONLY the exact primary name of a Music
   Assistant speaker entity" — it is asking for strings the model has never
   been shown. Confirmed from the other direction too: asked to play "on
   Sonos 2", tier 1 answers *"I don't see a speaker called Sonos 2 in your
   home"* — and it is telling the truth about its own context.

   The full failure chain, every step observed rather than inferred:

   1. User says "in the living room". Model picks the alias it can see:
      `player: "Sonos"`.
   2. The script cannot match aliases — a platform limitation this document
      already records (`entity_attr(id,'aliases')` does not exist).
   3. The area-name fallback cannot save it either: it matches the *area name*
      as a substring of the passed value, and "Sonos" contains no room name.
   4. The fail-loud guard returns `valid_speaker_names` — **primary** names,
      the ones the model has never seen — and `valid_rooms` as a **separate,
      unpaired list**.
   5. The model picks the entry that looks most like what it asked for:
      **"Sonos 2"**. That is a real, valid speaker, so the guard accepts it.
      It is in the **Dining Room**.

   So the wrong-room outcome is not a coin flip. It is the guard's own error
   payload steering the model to the nearest string, with no information about
   which room any of those names is in. The 1-in-3 rate is how often step 3
   fails, not how often the model "hallucinates".

   **This also explains the model's confident wrong belief about the layout.**
   In the same run it stated *"the speakers I can use are in the Living Room
   (Sonos 2 and Living Room Speaker)"* — pairing a primary name it learned
   from an error message with a room it guessed, because the two lists in the
   guard arrive unrelated to each other.

   **FIXED 2026-08-24, in three parts. 3 of 3 on the case that was 1 of 3.**

   The fix had to cover three distinct points in the chain, because fixing
   only one leaves the other two intact:

   1. **The guard returns pairs.** `valid_speaker_names` and `valid_rooms` —
      two lists with no relationship between them — are replaced by a single
      `speakers` list of `{name, room}`. The error text now says explicitly
      *"Retry with the name whose room matches the room the user asked for -
      do not pick by which name looks most like '<what you tried>'"*, because
      picking by string similarity is precisely how it reached "Sonos 2".
      Applied to `play_music`, `clarify_music_choice` and `start_dj_session`,
      which all carry the same guard.

   2. **The prompt stopped forbidding the one input that works.** This is the
      part that fixes the *first* attempt rather than the retry. Measured
      directly with `ha_eval_template` against the live resolver:

      | Value passed as `player` | Resolves to |
      | --- | --- |
      | `living room` / `the living room` | ✅ the Living Room speaker |
      | `dining room` | ✅ the Dining Room speaker |
      | `Sonos` (the alias the model sees) | ❌ unresolved |
      | `the Era` (ditto) | ❌ unresolved |

      **Room names resolve perfectly.** The area-name fallback was added
      deliberately for exactly this (see "Area name is the real fix surface"
      in the traps section) — and the prompt said *"never pass a room name,
      nickname or alias"*, steering the model away from the only form that
      works. The prompt and the script had been contradicting each other.
      The rule is now the reverse: when the user names a room, pass the room.

   3. **The guard has to be reachable.** With 1 and 2 live, "play … on Sonos
      2" still failed — the model refused *without calling any tool at all*,
      so a better guard could never be consulted. Added: *"If the user names
      a speaker you do not recognise, do NOT refuse and do NOT ask which one
      they mean. Call Play Music with the name they used anyway… Only say a
      speaker does not exist after a script has actually told you so."*

   **Verified, by trace and by player state, not by the spoken reply:**

   | Case | Before | After |
   | --- | --- | --- |
   | "play Fleetwood Mac in the living room" ×3 | 1 pass, 2 wrong room | ✅ **3 of 3**, each a single call with `player: "living room"`, no guard fire, no retry |
   | "play Take Five in the dining room" | not tested | ✅ resolved to the Dining Room speaker — the fix is not a living-room bias |
   | "play the album Rumours on Sonos 2" | ❌ refused a valid speaker, twice | ✅ plays on the Dining Room speaker |
   | Guard payload, called directly with a bogus name | two unpaired lists | ✅ `[{name, room}, …]`, nothing played |

   **What this does not do.** It does not make aliases resolvable — that is
   still a platform limitation. It routes around them by making the room the
   preferred input and by letting the guard correct anything else. A speaker
   whose alias is *not* a room reference and whose primary name the user does
   not know is still reachable only via the guard's retry.

   **The lesson worth carrying:** this defect survived four reproductions and
   several fixes aimed at the guard because it was described as "the model
   invents targets". It never invented anything. It passed the only name it
   had been shown, into a field documented to accept a different kind of name,
   and was then handed a list with the one piece of information it needed
   (which room) stripped out. **When a model keeps making the same "mistake",
   check what it can actually see before hardening the thing that rejects it.**

   **The structural fix this points at, not yet built:** `play_music`
   resolves a *room* to its speakers today only when `player` is omitted. If
   the model supplies a `player` while the utterance also names a room, the
   script has no cross-check. Validating a supplied `player` against the
   named room — and refusing or correcting on mismatch — would make this
   failure structurally impossible rather than probabilistically rare, which
   is the standard defect #1 above already argues for. That requires the
   room to reach the script, which it currently does not.

5. **A room with more than one speaker makes "resume" with a room-only
   target ambiguous, and it fails rather than guessing.** Observed 0/2 in
   the 2026-08-19 suite run. This is native HA pause/resume intent handling,
   not `search_music`/`play_music` — those scripts are never invoked for a
   bare resume. Recorded here because it surfaced in the same suite, not
   because it's this doc's territory to fix. Worth a second look only if it
   starts affecting named-target requests too, not just bare ones.

## Multi-room / synchronized playback

Works cross-brand with no setup. Joining with one speaker as leader pulls
the other onto the leader's queue; both report identical positions.

**Mechanism:** MA switches both players to **AirPlay** on join — the common
protocol, and one that carries timestamps. Vendor-native grouping only works
within a brand.

**Caveats:**

- AirPlay is a small quality downgrade versus a speaker's native protocol.
- Acoustically it was audibly out of sync, attributed to an analog output
  chain (line-out → external amp → speaker). HA's reported position is the
  *queue* position and cannot see downstream analog latency.
- MA's AirPlay provider offers a per-player fixed offset up to ±500 ms.
  **A lagging player cannot be advanced — the other player must be delayed.**
- Known MA failure mode: mismatched per-player DSP settings cause drift with
  no UI warning. Rule that out before hand-tuning offsets.
- **Not pursued:** not worth it for a speaker on an analog output. Revisit
  when speakers are digital end to end, where multi-room is essentially free.

## Deliberately not done

- **`prefer_local_intents` stays off.** With it on, a room-targeted request
  can *match* a local hassil template and then fail — and a matched-but-failed
  local intent does **not** fall through to the LLM. Do not enable without
  re-testing music end to end. (Audit this before trusting it; it has
  drifted to true on a non-preferred pipeline before.)
- **The missing model selector** in the OpenAI integration UI. Options do
  not render even with recommended settings off; upstream bug. This is a
  leading suspect for the non-determinism above, which is why OpenRouter
  exists as an alternative path.
- **A polling refill automation for DJ sessions.** Considered and not built:
  the media_player entity exposes no queue-depth attribute (checked), so a
  "queue running low" trigger would have to poll `music_assistant.get_queue`
  on a time pattern. The curated batch plus radio fill as a backstop covers
  the same need with no polling and no extra `ai_task` calls. Revisit only if
  sessions are observed running dry.
- **An upstream `home-assistant/core` PR to expose Spotify's popularity
  field.** Investigated in full before the search/play redesign was chosen
  instead. Music Assistant's Spotify provider already parses `popularity`
  into `track.metadata.popularity` — confirmed by reading MA's own source,
  not assumed — but the official HA integration's response-building
  function (`media_item_dict_from_mass_item` in
  `homeassistant/components/music_assistant/schemas.py`) flattens
  `metadata.explicit` into every search result and never touches
  `metadata.popularity`. The fix really is that small — three lines,
  mirroring the existing `explicit` pattern. Rejected anyway: it only ever
  helps Spotify-sourced collisions (Apple Music's Catalog API has no
  popularity field for any consumer to read, upstream or not), it depends
  on an external repo's review timeline, and model judgment (see "Why two
  scripts") covers both providers today with no external dependency.
  Revisit only if model judgment turns out to be unreliable at scale — it
  is not a fix for defect #3b (phonetic mismatches) either way, so it
  would only ever be a partial answer to a problem the current design
  already answers more completely.

## How to audit this

Regenerate everything this doc no longer states:

- **Speakers, rooms, exposure, aliases, duplicates** — `ha_get_entity` on
  the media players; check `platform`, `area_id`, `hidden_by`, and the
  conversation exposure option. Two entities sharing a `unique_id` are the
  same speaker.
- **Which speakers MA actually owns** — filter media players by
  `platform == "music_assistant"`; an unavailable one still counts and loses
  its attributes, so do not filter on attributes.
- **Areas and floors** — `ha_list_floors_areas`
- **That every room still has exactly one music speaker** — group the MA
  players by `area_name` after removing anything labelled `no_music`. Two in
  one room makes "play in that room" arbitrary; see "Room targeting is only
  as good as one-speaker-per-room".
- **What the `no_music` label currently excludes** — `label_entities('no_music')`.
  Area assignments and labels are **not** in `ha_export/`, so this is the only
  record of them.
- **Conversation agents that exist** — the `conversation` domain in the
  state machine
- **Which agent and wake word each pipeline uses** — `ha_manage_pipeline`
- **Current script bodies and `config_hash`** — `ha_config_get_script` for
  `search_music`, `play_music`, `clarify_music_choice`, and
  `set_music_repeat`, read immediately before any write
- **Which model the conversation agent is currently using** — the
  OpenRouter conversation subentry's `model` field
  (`ha_get_integration(entry_id=..., include_subentries=true)` then
  inspect the subentry; the subentry's display title is not reliably kept
  in sync with the model field — confirmed stale once already, don't trust
  it). As of 2026-08-19 this should read `anthropic/claude-haiku-4.5` — a
  Sonnet 5 experiment was tried and reverted, see "The recall-boost
  mechanism" above.
- **MA config entry ID** (needed as a literal in service calls) —
  `ha_get_integration(query="music assistant")`. It is also hardcoded as a
  literal inside `search_music` and `dj_queue_tracks`; both need updating if
  the entry is ever recreated.
- **DJ session state** — the five `dj_session_*` helpers. `dj_session_active`
  should be `off` and `dj_session_brief` empty whenever no session is running;
  a stuck `on` means a session ended without `stop_dj_session` and `steer`
  will act on a dead session.
- **Which AI Task entity curation uses, and whether Anthropic works yet** —
  `ha_call_service('ai_task', 'generate_data', ...)` with any `structure`
  against the Anthropic entity. If it no longer 400s on
  `additionalProperties`, the upstream bug is fixed and curation can move off
  OpenAI.
- **That the worker stayed unexposed** — `ha_get_entity_exposure` on
  `script.dj_queue_tracks`; it must be false, or its ten-field schema is
  billed on every utterance in the house.
- **Provider errors, rate limits, playback locks** — the MA add-on log;
  these never appear in the HA log

## Debugging playbook

Closed-loop, no speaking required:

- **Send an utterance:** `conversation.process` with the agent ID and
  `return_response: true`. The LLM round-trip often exceeds the tool timeout
  — not a failure, re-read state.
- **Verify with state, never the spoken reply.** The model reports actions
  it did not take. Check the media title and content ID on the target player.
- **Did the model call the tool(s) at all?** Check `last_triggered` on
  `search_music` and `play_music`, or look for them in the
  `conversation.process` result. Neither firing means the model used a
  built-in intent instead. `search_music` firing without `play_music`
  following means it either found nothing usable or declined to act on
  what it found — check its trace for which.
- **What did it decide?** Execution traces show every resolution value and
  which `choose` branch fired. This is how the enum bug, the
  playlist-over-track bug, and a media-type misclassification were all found.

Guessing has been consistently slower than probing. Across sessions, every
diagnosis formed by inference rather than a trace or a log has been wrong —
including several about what the *tooling* could do, not just the system.
Reach for the trace first.
