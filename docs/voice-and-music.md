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

**Now closed, 2026-08-21 — the combination finally landed.** The suite run
of that date caught the exact pairing this paragraph asked for: rep 3 of
"play Fleetwood Mac in the living room" resolved to the Dining Room speaker,
and the reply said *"Now playing Fleetwood Mac Radio on the Sonos."* The
routing was wrong and **the reply was honest about it** — it named the
speaker the tool actually returned rather than echoing the room the user
asked for. Compare the pre-fix failure, where the same wrong-player
situation was reported as the *right* room. The grounding fix therefore
holds in the case it had never been observed in: it does not prevent
mis-routing, but it stops mis-routing from being reported as success.

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
the model to use only that data) is therefore the ceiling of what's
achievable here, not a stopgap on the way to something stronger — reflect
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
| Named device | "play [artist] on the [device]" |
| STT variant | mangled artist name, as speech-to-text would garble it |
| Room with no speaker | must refuse, not substitute |
| Nonsense query | must not fuzzy-match to real content |
| Pause, no target | |
| Pause, with target | |
| Resume, with target | |
| Repeat, new content | "play [song] on repeat" — must both play the right song AND set `repeat: "one"` in the same request |
| Repeat, current, no target | "put this song on repeat" / "repeat this" — no room named; this is the exact phrasing that triggered defect #4 during this feature's own verification, see "Repeat" above |
| Repeat, remove | "stop repeating" / "turn off repeat" |
| Open-ended, no content | "play some music" / "put something on" — must play something and set `radio_mode`, never ask what they want |
| Open-ended, vibe | "play something like [artist]" — `radio_mode` true |
| Specific request stays specific | "play the album [name]" — `radio_mode` must **not** be set; this is the over-triggering case |
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

4. **The model sometimes hallucinates a `player` value that doesn't exist**,
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
- **LLM-curated tracklists ("DJ mode" stage 2).** Investigated alongside the
  radio-mode passthrough above and deliberately deferred, not rejected. Prior
  art exists and is worth reading before building: Music Assistant's own
  **Don't Stop The Music** (auto-enables radio fill when the queue drains)
  and its **Sonic Similarity** plugin (local audio analysis, works on
  filesystem libraries where providers supply no similarity data), plus a
  community integration that defines "stations" as text prompts, resolves
  generated tracks through `music_assistant.play_media`, and refills on a
  queue-low trigger. **That project's author started on DSTM and moved off
  it** because it drifted and duplicated on niche stations — inherit that
  negative result rather than re-deriving it. The two decisions already made
  for a future stage 2, so they are not relitigated: suppress `music_played`
  during a curated session (see "Open-ended playback" for why), and verify
  each resolved candidate against what was asked before enqueuing it rather
  than trusting the generated title. The open unknown is what fraction of a
  generated batch survives that verification — measure it on the first batch
  rather than guessing, since a strict match could starve the queue.
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
  `ha_get_integration(query="music assistant")`
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
