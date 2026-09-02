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
- **TLS interception breaks the run before it starts.** A proxy that rewrites
  certificates produces `CERTIFICATE_VERIFY_FAILED: Basic Constraints of CA
  cert not marked critical`, which looks like an endpoint problem and is not.
  Test the trust store against any public host before blaming Meta.

### What was and was not verified

Verified: the HA-side analysis end to end on synthetic clips (a hesitation
clip cut at the silence window, a long clip reaching the ceiling, a fluent
clip untouched); relaxed VAD sensitivity rescuing the hesitation case while
leaving the ceiling untouched; the WebSocket client driven through a full
session against a local mock — handshake shape, real-time pacing, complete
audio delivery, `endStream` half-close, and event folding.

**Not verified: any call to Meta's actual endpoint.** The sandbox this was
built in intercepts TLS, so the live API was never reached. The protocol is
implemented from the vendor's published reference and should be treated as
unconfirmed until a real key runs a real clip.

## What a "yes" looks like

Decide on two numbers, not on transcript quality:

1. **Rescued turns** — of the clips HA would truncate, how many does Muse
   carry to the real end of the sentence. If this is near zero on honest
   household audio, the feature does not exist and nothing should be built.
2. **Endpoint lag** — wall-clock delay from the speaker stopping to the text
   arriving. HA's comparable figure is its 0.7s timer plus the STT round
   trip, so lag meaningfully under that is a straight win and lag well above
   it trades one annoyance for another.

If both land well, the build is one `stt` platform entity declaring
`requires_external_vad=False`, a config flow, and the key in
`secrets.yaml` — small, but on an untrodden HA code path.

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
