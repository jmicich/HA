# Voice & Music — design decisions

Status as of 2026-08-18. Covers the Assist voice stack, `script.play_music`,
and Music Assistant playback. Read this before changing the pipelines, the
conversation agent's prompt, or the script.

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

## script.play_music

Exposed to Assist, so the model sees it as a tool. Its description and field
descriptions **are** the tool schema — editing them changes model behaviour.
Treat them as code.

Fields: `query` (required), `media_type` (optional), `player` (optional),
`artist` (optional).

### Why media_type exists

The script cannot tell a track from an artist from a bare query string. A
fixed type priority therefore always gets one of them wrong: with playlist
ranked first, "play [song] by [artist]" played an artist playlist that
opened with a different song.

The model knows the intent, so it declares it. Intent extraction belongs to
the model; resolution belongs to the script.

### Resolution order

Explicit (when `media_type` is set):

1. track → best track
2. album → best album
3. playlist / radio → best playlist
4. artist → **playlist, then album** (never artist directly — see traps)

Fallback (when `media_type` is unset, i.e. a genre or mood):

1. playlist → album → track → **artist last resort only**

Provider ranking within every type: **library → Spotify → Apple Music**.
BBC Sounds URIs are filtered out entirely — they return spoken-word
documentaries in the tracks bucket.

Every successful branch sets a `result` containing the resolved item's name
and kind, fires a `music_played` event carrying it, then stops with a
response variable. See `music-recall-memory.md` for what consumes that event.

## Traps, with evidence

**Artist-type playback triggers unbounded enumeration.** MA fans out API
calls to fetch all tracks for an artist, tripping the provider rate limiter
with repeating backoff. Playback never starts and the player falls to idle.

**This is true of library artists too.** Getting it wrong twice cost real
time. A library artist aggregates *provider mappings*, so enumerating one
still hits the streaming provider. There is no safe artist-type playback —
treat artist as last resort regardless of provider prefix. Playlists and
albums are bounded fetches.

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
entity selector wants entity IDs. The script resolves both.

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

## Regression suite

Run via `conversation.process` with `return_response: true`; verify with a
state read, not the spoken reply. **Run each case at least three times** —
several failures here are probabilistic.

Pass/fail status is deliberately not recorded: it is a measurement with a
short shelf life, and a stale one invites false confidence.

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
3. **No relevance threshold.** Nonsense queries fuzzy-match to real content.
   Search essentially always returns *something*, so the script's "no music
   found" path is unreachable in practice. Proposed mitigation: have the
   script return the resolved item's name so the model reports what actually
   played instead of parroting the query. (The event payload now carries
   this; the response path does not yet use it.)

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
   friendly name rather than by omission. Each hallucinated attempt still
   costs a real `music_assistant.search` call, so this eats into the
   rate-limit budget even when it self-corrects. The refuse-on-unknown-name
   guard is doing its job here; the gap is upstream, in why the model
   invents a target at all.

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
- **Current script body and `config_hash`** — `ha_config_get_script`, read
  immediately before any write
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
- **Did the model call the tool at all?** Check `last_triggered` on the
  script, or look for it in the `conversation.process` result. Absence means
  the model used a built-in intent instead.
- **What did it decide?** Execution traces show every resolution value and
  which `choose` branch fired. This is how the enum bug, the
  playlist-over-track bug, and a media-type misclassification were all found.

Guessing has been consistently slower than probing. Across sessions, every
diagnosis formed by inference rather than a trace or a log has been wrong —
including several about what the *tooling* could do, not just the system.
Reach for the trace first.
