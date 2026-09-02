# Speech-to-text endpointing — who decides when you stop talking

Investigated 2026-09-02, prompted by Meta releasing a real-time ASR model the
day before. **Nothing has been built.** This records what the architecture
actually allows, what the probe measures, and the traps found on the way, so
that the build/don't-build decision is made against measurements rather than
launch-day benchmarks.

**No auditable state recorded here** — no version numbers, entity IDs, or
pass/fail figures. See "How to audit this".

## The question

Not "is Muse a better transcriber". The leaderboards answer that, and it is
the wrong question for this house. Ours is:

> When someone pauses mid-sentence, who decides the turn is over?

Today the answer is Home Assistant, and it decides on a timer.

## What Home Assistant does, and where the ceiling is

`assist_pipeline/vad.py` runs a `VoiceCommandSegmenter` over the audio
*before* any STT engine sees it:

| Constant | Value | Effect |
| --- | --- | --- |
| `silence_seconds` | 0.7 | Silence after which the turn is cut |
| `command_seconds` | 1.0 | Minimum turn length |
| `timeout_seconds` | 15.0 | Hard ceiling on a single turn |

**The 15-second ceiling is not reachable from any configuration surface.**
`pipeline.py` constructs the segmenter passing `silence_seconds` alone;
`timeout_seconds` keeps its default. The VAD sensitivity selector moves only
the first row (aggressive 0.25s, default 0.7s, relaxed 1.25s).

So the binding constraint on everyday use is not the ceiling — it is the
0.7-second silence timer, which is short enough that thinking mid-sentence
ends your turn. That is the behaviour worth measuring.

## The hook that makes a different answer possible

`stt.SpeechAudioProcessing` carries:

```
requires_external_vad: bool
"""True if an external voice activity detector (VAD) is required.

If False, the speech-to-text entity must detect the end of speech itself.
"""
```

and the pipeline only builds a segmenter when the provider asks for one:

```python
if (self.audio_settings.is_vad_enabled
        and self.stt_provider.audio_processing.requires_external_vad):
    stt_vad = VoiceCommandSegmenter(silence_seconds=...)
```

Declare `False` and **both** limits disappear together — the silence timer
and the ceiling. Home Assistant has a first-class seam for engines that do
their own endpointing.

**Trap: the seam is untrodden.** Searching HA's source for that flag returns
its definition, the pipeline's read of it, tests, and one integration —
HA Cloud's STT, which declares `requires_external_vad=True` directly beneath
a comment reading *"STT v2 detects the end of speech itself."* Nothing in the
codebase sets it to `False`. Whether that is deliberate caution or an
oversight is unknown; either way, anything built on this path is the first
production user of it, and that risk belongs in the estimate.

## What Muse Voice Transcribe is, and two things widely reported wrong

Meta Superintelligence Labs, released 2026-09-01. Streaming ASR with speaker
diarization and endpointing in one model. Speech-to-text only.

- **It is not on OpenRouter, and structurally cannot be.** OpenRouter routes
  chat completions; this is an audio WebSocket protocol. Meta's *text* Muse
  models (Spark, Glimmer) are on OpenRouter and are reachable from the
  existing integration — the voice model is a different thing entirely.
- **It is not OpenAI-compatible, despite the coverage saying so.** Several
  outlets report "point your OpenAI SDK at it and change the base URL". The
  vendor's own reference documents a proprietary surface: a realtime
  WebSocket and a file-transcription POST, with the access token carried in a
  JSON handshake frame rather than an `Authorization` header. There is no
  `/v1/audio/transcriptions`. The OpenAI-compatibility claim is true of Meta
  Model API's **text** models and has been over-generalised to the ASR
  endpoint. Anyone who trusts the press here loses an evening.

That second point is why this needs a custom component rather than a config
change: HA's OpenAI integration does register `stt` and `tts` platforms, but
its entry offers no base-URL override, and the protocol would not match if it
did.

## What this does *not* unlock

Worth stating plainly, because all three are easy to assume.

- **Multi-turn conversation already exists and is unrelated to STT.**
  `conversation.ConversationResult.continue_conversation` is acted on by the
  pipeline, which remembers the agent so the next turn skips the wake word.
  If the goal is *more turns*, that is a prompt change available today at no
  cost. Muse changes turn **depth**, not turn count.
- **Diarization is not speaker identification.** It separates voices within a
  stream; it does not say who they are. Per-person memory stays blocked for
  the reason `music-recall-memory.md` already records.
- **No text-to-speech.** Piper is unaffected.

## The probe

`scripts/probe_stt.py`. It reads WAV files and talks to Meta. **It cannot
touch Home Assistant** — no deploy, no reload, no state read — by design.

It reports, per clip, where HA would have stopped listening and where Muse
says the sentence ended, plus the wall-clock delay between the audio ending
and the text arriving. A clip where the two agree proves nothing. A clip
where HA truncates and Muse does not is the entire value proposition.

```
python scripts/probe_stt.py --clips recordings/            # HA side only, no key
python scripts/probe_stt.py --clips recordings/ --muse     # adds Muse
python scripts/probe_stt.py --help-recording               # how to capture audio
```

The API key is read from `META_API_KEY` only — never a command-line argument,
never echoed into the report or the JSON. A test asserts the last part.

### Traps the probe itself carries

- **HA's own debug recordings are already truncated.** Setting
  `debug_recording_dir` gives you exactly the audio the STT engine received —
  which is audio HA already cut at its VAD boundary. Perfect for confirming a
  truncation happened, useless for showing what was lost. The comparison
  needs **untruncated** recordings of the same utterances, captured
  separately. `--help-recording` says this too, because it is the mistake
  most likely to produce a confidently wrong answer.
- **Exact parity needs `pymicro-vad`, which will not install on Windows.**
  It is what HA pins, it ships no wheels, and it needs a C++ toolchain. Where
  it is missing the probe substitutes an energy gate and prints a warning on
  every report. Treat those runs as indicative; run on the instance, or on
  Linux, for a decision-grade number.
- **A percentile noise floor is the wrong way to build that fallback.** The
  first version took the 20th percentile of frame energy as the room's noise
  floor. On a dictation clip that is ~94% speech, the 20th percentile lands
  *inside speech*, putting the threshold above the median speech level: nine
  seconds of continuous talking was reported as a 2.2-second turn. Replaced
  with Otsu's method plus a guard for clips with no bimodality to find. The
  lesson generalises — any statistic that assumes what fraction of the input
  is signal will invert on the inputs that matter most.
- **TLS interception breaks the run before it starts, and the error blames
  the wrong thing.** Where a proxy or antivirus inspects TLS, Python's bundled
  OpenSSL rejects the intercepting CA that Windows and curl both accept:
  every connection fails with `CERTIFICATE_VERIFY_FAILED` while the browser
  works fine, which reads as an endpoint outage. Confirm by trying any
  unrelated public host — if google.com fails too, it is not Meta. The probe
  uses `truststore` to verify through the platform store, which resolves it.
  **Verification is never disabled**; a probe that skips it would be
  reporting numbers from a connection nobody should trust.

### What was and was not verified

Verified without the API: the HA-side analysis end to end (a hesitation clip
cut at the silence window, a long clip reaching the ceiling, a fluent clip
untouched); relaxed VAD sensitivity rescuing the hesitation case while
leaving the ceiling untouched; the WebSocket client driven through a full
session against a local mock.

Verified against the live endpoint: handshake, bearer token in the JSON
frame, TLS through the OS trust store, real-time pacing, `endStream`
half-close, and the documented event schema. The protocol as published is
correct.

**Still not representative: the audio.** The only speech run through it so
far was generated with Windows SAPI text-to-speech — clean, close-mic, no
room noise, and an artificial silence for the pause. Every number below is a
smoke test, not a measurement of this house.

## First live run, and the result that inverts the premise

The framing this document opened with — *would Muse let someone finish a
sentence HA cuts off* — did not survive contact with the API.

Given one utterance with a 900ms mid-sentence pause:

- **HA** cuts at 3.33s and loses the entire second clause.
- **Muse in ENDPOINTING mode endpoints at 3.02s** — *earlier* than HA, in
  materially the same place — and emits the utterance as **two turns**.
- **Muse in PUSH_TO_TALK mode transcribes straight through the pause** and
  returns the whole sentence as one transcript.

So Muse does not "keep listening" through a hesitation. It segments there
too. What it does differently is keep the session open and deliver the rest
as a further turn, rather than discarding it.

**And the split is not tunable.** The realtime handshake schema has no
parameter for endpointing sensitivity, silence tolerance, pause duration, or
minimum turn length. Turn-splitting behaviour is fixed by the model.

### Why that matters architecturally

Home Assistant's STT entity returns **one transcript per turn**. Both modes
therefore fail on their own:

| Mode | Behaviour | Why it does not drop in |
| --- | --- | --- |
| `ENDPOINTING` | Splits at the pause | HA takes the first transcript; later turns have nowhere to go |
| `PUSH_TO_TALK` | Never splits | Nothing decides when to stop — that was HA's VAD, which is what we removed |

The path that could still work is a component that declares
`requires_external_vad=False` — so HA never cuts the stream — runs
`ENDPOINTING`, and **joins turns itself** under its own, longer silence rule
before returning. The probe confirms the raw material is there: both halves
transcribe correctly, with timestamps.

**The cost is latency, and no model removes it.** To tolerate a 1.5s
hesitation you must wait 1.5s past the last turn before returning, because
nothing can know whether someone has finished thinking. Muse's endpointing is
sold as semantic, and here it split before the word "and" — a clear
continuation — so it does not obviously buy tolerance for free. That may be
an artefact of synthesised speech, which carries none of the prosody a real
speaker trailing off would; it is a question for honest audio, not a settled
negative.

### Three artefacts the harness reported as results

All three were caught by running it, all three are now regression-tested, and
they share one shape: **a number that looks like a finding and is a property
of the measuring apparatus.**

1. **Trailing silence scored as truncation.** "HA stopped before the file
   ended" marks every cleanly-finished sentence as a loss, since recordings
   end in silence. Now counts only speech after the cut.
2. **Multi-turn transcripts were overwritten, not joined.** Keeping the last
   `speechComplete` silently dropped the first half of the utterance and
   printed a fluent-looking half-sentence. Now joined.
3. **PUSH_TO_TALK always "beat" HA.** Its endpoint is wherever the harness
   half-closed, so it outlasted HA's cut by construction. Runs where the
   model never endpointed are now excluded from scoring and labelled.

## What a "yes" looks like

The first number has to be restated in light of the above. Decide on:

1. **Rescued turns** — clips where Muse's *first* endpoint lands
   meaningfully later than HA's cut. Measured against synthetic speech this
   was **zero**; Muse agreed with HA about where the sentence ended.
2. **Recoverable turns** — clips Muse split but transcribed completely.
   These are only a win if the component joins them, which is design work,
   not configuration.
3. **Endpoint lag** — wall-clock delay from the first endpoint to its text.
   Roughly 0.58s in ENDPOINTING against HA's 0.7s timer plus round trip, so
   not a regression on this evidence.

**On present evidence the case for building is weak**, and the honest next
step is to re-run against real household recordings before writing any
integration. If real speech shows the same agreement between Muse and HA on
where a turn ends, then the only thing this buys is transcription accuracy,
which is a much smaller prize than the one this investigation started with.

## How to audit this

- **Do HA's VAD constants still hold** — read `assist_pipeline/vad.py` in the
  running version; `tests/test_probe_stt.py` pins them and fails if the port
  drifts.
- **Does the endpointing seam still exist** — grep the installed
  `stt/models.py` for `requires_external_vad`, and `assist_pipeline/
  pipeline.py` for its only read.
- **Has any integration started using it** — search HA's source for the flag
  set to `False`; today none does, and that changing is the single best
  signal that this path has become safe.
- **Which pipeline is live, and what STT it uses** — list the Assist
  pipelines and read the preferred one's `stt_engine`.
- **What the OpenRouter entry can actually select** — read its conversation
  subentry schema; the model field is a validated dropdown and rejects
  anything not in it.
- **Current Muse pricing and model ID** — the vendor's model page. Both were
  days old when this was written.
