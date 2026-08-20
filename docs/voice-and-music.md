# Voice & Music — design decisions

Status as of 2026-08-19. Covers the Assist voice stack,
`script.search_music` + `script.play_music`, and Music Assistant playback.
Read this before changing the pipelines, the conversation agent's prompt, or
either script.

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
`name` (required), `artist` (optional), `player` (optional). Plays exactly
the candidate it's given via `music_assistant.play_media`. It does no
ranking or resolution of its own — that already happened in `search_music`
and, before that, in the model's choice of candidate. Fires the
`music_played` event with `played`/`kind`/`artist`, then stops with a
response variable. See `music-recall-memory.md` for what consumes that
event.

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
| Mangled recall | garbled version of a previously played item → maps to it |
| Novel request | absent from the recall list → treated as new, **must not** bend |

The last two exist because of the recall list; see `music-recall-memory.md`.

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

   **3b. Phonetic or garbled-transcription mismatches — still open, and
   confirmed deterministic, not probabilistic.** The mangled-recall case
   ("Roomers" → should resolve to "Rumours") failed 0/3 in the 2026-08-19
   suite run, the same wrong way each time, *even with the correct answer
   present twice over* — once as a properly-artist-tagged `search_music`
   candidate, once by name in the recall-list prompt hint. See the new trap
   above ("Having the right answer in context does not mean the model uses
   it"). Model judgment does not generalize to this case the way it did to
   3a — the failure isn't a missing signal, it's that literal text
   similarity outweighs a correct hint already in context. No fix identified
   yet; this needs either a stronger prompt treatment specifically for
   phonetic mismatches, or a different mechanism entirely (e.g. resolving
   against the recall list *before* search, not just hinting during it).

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
  both `search_music` and `play_music`, read immediately before any write
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
