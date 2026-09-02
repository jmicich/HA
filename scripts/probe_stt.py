#!/usr/bin/env python3
"""Measure whether Meta's Muse Voice Transcribe is worth an integration.

This answers one question and deliberately not others: **would handing
end-of-speech detection to the STT engine let someone finish a sentence that
Home Assistant currently cuts off?**

The leaderboards already say Muse transcribes long-form audio well. That is
not our workload. Ours is a person in a kitchen saying a sentence with a
pause in the middle of it, and the thing that decides whether the assistant
feels usable is not word error rate — it is *where the turn was cut*.

Home Assistant decides that today, upstream of whatever STT engine is
configured. `assist_pipeline/vad.py` runs a `VoiceCommandSegmenter` with:

    silence_seconds  = 0.7    # ends the turn (VAD sensitivity moves this)
    command_seconds  = 1.0    # minimum turn length
    timeout_seconds  = 15.0   # hard ceiling, NOT configurable from anywhere

`pipeline.py` constructs that segmenter with `silence_seconds` alone, so the
15-second cap cannot be reached from any config surface. But it only
constructs it at all when the STT provider says it needs one:

    if (self.audio_settings.is_vad_enabled
            and self.stt_provider.audio_processing.requires_external_vad):

`stt.SpeechAudioProcessing.requires_external_vad` is documented as "If False,
the speech-to-text entity must detect the end of speech itself." Declaring
False removes the segmenter entirely — both the 0.7s silence cut and the 15s
ceiling. That flag exists in 2026.8.2, and no shipping integration sets it to
False, so anything built on it is walking new ground.

So the probe measures two cut points on the same audio:

  * where **HA** would have stopped listening, and
  * where **Muse** says the sentence actually ended,

plus the latency each implies. A clip where both agree tells us nothing. A
clip where HA truncates and Muse does not is the entire value proposition,
and counting those is the point of this script.

    # No API key needed: shows where HA would cut each clip.
    python scripts/probe_stt.py --clips recordings/

    # Adds the Muse side. Reads META_API_KEY from the environment.
    python scripts/probe_stt.py --clips recordings/ --muse

    # Speaker separation instead of endpointing.
    python scripts/probe_stt.py --clips recordings/ --muse --mode DIARIZATION

**Nothing here touches Home Assistant.** It reads WAV files and talks to
Meta's API. It cannot deploy, reload, or change any part of the instance,
by design — this is a measurement, made before deciding whether to build.

Getting representative audio matters more than anything this script does.
Speech recorded into a laptop microphone is not the workload; the satellite's
far-field microphone in a room with a dishwasher is. See `--help-recording`.

The API key is read from the environment only, never a command-line argument
(which would land in shell history) and never echoed into output or JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import struct
import sys
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

# Home Assistant's pipeline resamples everything to this before the VAD sees
# it (assist_pipeline/const.py SAMPLE_RATE), so the HA-side analysis must run
# here regardless of what the source file is.
HA_SAMPLE_RATE = 16000
HA_SAMPLE_WIDTH = 2

# pymicro_vad wants exactly 10ms per call.
VAD_CHUNK_MS = 10

MUSE_ENDPOINT = "wss://api.meta.ai/v1/asr/realtime"
MUSE_MODEL = "muse-voice-transcribe-1.0"

# Meta rejects ingress that runs below real time (close code 1008), so frames
# are paced against a monotonic clock rather than sent as fast as possible.
SEND_FRAME_MS = 20

RECORDING_HELP = """\
How to capture audio that is actually representative
====================================================

The probe is only as good as its input. A clip recorded by leaning into a
laptop microphone will make any engine look good and will not predict how
either behaves on the satellite.

Home Assistant can hand you exactly the audio its STT engine received.
Add to configuration.yaml on the instance:

    assist_pipeline:
      debug_recording_dir: /config/pipeline_debug

Restart, speak to the satellite, then collect the WAVs from
  <debug_recording_dir>/<device_id>/<pipeline_name>/<run_id>/01_stt-*.wav

**Read this caveat before drawing conclusions from those files.** HA writes
the recording from inside `_speech_to_text_stream`, which stops when the VAD
breaks the loop — so a debug recording is *already truncated at HA's cut
point*. It tells you what HA heard, and is therefore perfect for confirming
the truncation is real, but it cannot show you what was lost after the cut.

For the comparison this script exists to make you need **untruncated** audio
of the same utterances: record the full sentence separately (any recorder,
16-bit mono WAV), including the hesitation that gets you cut off. Then the
HA column below reconstructs where HA would have stopped, and the Muse column
shows where the sentence really ended.

Turn off debug_recording_dir when finished. It writes every utterance in the
house to disk indefinitely.
"""


# --------------------------------------------------------------------------
# Audio loading
# --------------------------------------------------------------------------


@dataclass
class Clip:
    """A loaded, normalised mono 16-bit PCM recording."""

    path: Path
    pcm: bytes
    rate: int

    @property
    def duration_s(self) -> float:
        return len(self.pcm) / (self.rate * HA_SAMPLE_WIDTH)


def _resample_linear(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Resample 16-bit mono PCM by linear interpolation.

    Deliberately simple: `audioop` was removed in Python 3.13 and pulling a
    DSP dependency in for a measurement harness is not worth it. Linear
    interpolation is not what a real integration should ship, but the
    difference does not move a VAD decision or a transcript.
    """
    if src_rate == dst_rate:
        return pcm

    src = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    if not src:
        return b""

    ratio = src_rate / dst_rate
    out_len = int(len(src) / ratio)
    out = []
    for i in range(out_len):
        pos = i * ratio
        lo = int(pos)
        hi = min(lo + 1, len(src) - 1)
        frac = pos - lo
        out.append(int(src[lo] * (1.0 - frac) + src[hi] * frac))
    return struct.pack(f"<{len(out)}h", *out)


def load_clip(path: Path, target_rate: int) -> Clip:
    """Read a WAV file as mono 16-bit PCM at target_rate."""
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())

    if width != 2:
        raise ValueError(
            f"{path.name}: {width * 8}-bit audio; this probe needs 16-bit PCM"
        )

    if channels == 2:
        # Average the channels rather than dropping one; a satellite array
        # mixed to stereo would otherwise lose half its signal.
        samples = struct.unpack(f"<{len(frames) // 2}h", frames)
        mixed = [
            (samples[i] + samples[i + 1]) // 2 for i in range(0, len(samples) - 1, 2)
        ]
        frames = struct.pack(f"<{len(mixed)}h", *mixed)
    elif channels != 1:
        raise ValueError(f"{path.name}: {channels} channels; need mono or stereo")

    return Clip(path=path, pcm=_resample_linear(frames, rate, target_rate),
                rate=target_rate)


# --------------------------------------------------------------------------
# The Home Assistant side
# --------------------------------------------------------------------------


class SpeechDetector:
    """Per-10ms speech probability, matching what HA feeds its segmenter."""

    exact: bool = False
    name: str = "unknown"

    def probability(self, chunk: bytes) -> float:
        raise NotImplementedError


class MicroVadDetector(SpeechDetector):
    """The real thing: the same pymicro-vad HA pins in its manifest."""

    exact = True
    name = "pymicro-vad (exact HA parity)"

    def __init__(self) -> None:
        from pymicro_vad import MicroVad  # noqa: PLC0415 - optional dependency

        self._vad = MicroVad()

    def probability(self, chunk: bytes) -> float:
        return self._vad.Process10ms(chunk)


# Below this RMS nothing is treated as speech regardless of what the
# threshold search returns; it is roughly the level of a quiet room.
ABSOLUTE_SILENCE_RMS = 120.0

# A clip whose loud and quiet frames are within this ratio has no real
# silence in it to find, so splitting it would invent one.
HOMOGENEOUS_DYNAMIC_RANGE = 4.0


def _otsu_threshold(levels: list[float]) -> float:
    """Split frame energies into speech and silence by maximising variance.

    A fixed percentile cannot do this job: it assumes how much of the clip is
    speech, and gets the answer badly wrong when that assumption breaks (a
    dictation clip that is 94% speech puts the 20th percentile *inside*
    speech, chopping continuous talking into fake silences). Otsu's method
    makes no such assumption — it finds the split that best separates the two
    populations, whatever their relative sizes.
    """
    if not levels:
        return ABSOLUTE_SILENCE_RMS

    ordered = sorted(levels)
    quiet = ordered[len(ordered) // 20]
    loud = ordered[len(ordered) * 19 // 20]

    if loud < ABSOLUTE_SILENCE_RMS:
        return float("inf")  # the whole clip is silence
    if loud < max(quiet, 1.0) * HOMOGENEOUS_DYNAMIC_RANGE:
        # No bimodality to find: classify on absolute level instead of
        # manufacturing a boundary inside one continuous population.
        return ABSOLUTE_SILENCE_RMS

    lo, hi = math.log10(max(quiet, 1.0)), math.log10(loud)
    bins = 64
    histogram = [0] * bins
    for level in levels:
        position = (math.log10(max(level, 1.0)) - lo) / (hi - lo)
        histogram[min(bins - 1, max(0, int(position * bins)))] += 1

    total = len(levels)
    sum_all = sum(i * count for i, count in enumerate(histogram))
    weight_bg = 0
    sum_bg = 0.0
    best_variance, best_bin = -1.0, 0

    for i, count in enumerate(histogram):
        weight_bg += count
        if weight_bg == 0:
            continue
        weight_fg = total - weight_bg
        if weight_fg == 0:
            break
        sum_bg += i * count
        mean_bg = sum_bg / weight_bg
        mean_fg = (sum_all - sum_bg) / weight_fg
        variance = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
        if variance > best_variance:
            best_variance, best_bin = variance, i

    threshold = 10 ** (lo + (best_bin + 1) / bins * (hi - lo))
    return max(threshold, ABSOLUTE_SILENCE_RMS)


class RmsDetector(SpeechDetector):
    """Fallback when pymicro-vad will not build.

    pymicro-vad ships no wheels, so on a machine without a C++ toolchain
    (a Windows dev box, for instance) it cannot be installed at all. This
    stands in with an energy gate whose threshold is found by Otsu's method.

    **It is an approximation and is labelled as one everywhere it is used.**
    It gets silence boundaries roughly right, which is most of what the
    segmenter keys on, but it is not HA's decision and must not be reported
    as though it were.
    """

    exact = False
    name = "RMS approximation (pymicro-vad unavailable)"

    def __init__(self, pcm: bytes) -> None:
        levels = [_rms(f) for f in _iter_chunks(pcm, HA_SAMPLE_RATE, VAD_CHUNK_MS)]
        self._threshold = _otsu_threshold(levels)

    def probability(self, chunk: bytes) -> float:
        # Binary rather than graded: an energy gate has no honest claim to a
        # confidence, and both of HA's thresholds (0.2 and 0.5) sit between.
        return 1.0 if _rms(chunk) > self._threshold else 0.0


def _rms(chunk: bytes) -> float:
    if not chunk:
        return 0.0
    samples = struct.unpack(f"<{len(chunk) // 2}h", chunk)
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def _iter_chunks(pcm: bytes, rate: int, chunk_ms: int) -> list[bytes]:
    size = int(rate * HA_SAMPLE_WIDTH * chunk_ms / 1000)
    return [pcm[i : i + size] for i in range(0, len(pcm) - size + 1, size)]


@dataclass
class VoiceCommandSegmenter:
    """A faithful port of homeassistant.components.assist_pipeline.vad.

    Ported rather than imported because Home Assistant is not a dependency of
    this repo's tooling and installing it to read sixty lines of arithmetic
    would be worse. Source: home-assistant/core, assist_pipeline/vad.py
    (Apache-2.0), read at tag 2026.8.2.

    **Keep this in sync deliberately.** If HA changes these constants the
    probe silently starts measuring a pipeline that no longer exists;
    tests/test_probe_stt.py pins the values that matter.
    """

    speech_seconds: float = 0.3
    command_seconds: float = 1.0
    silence_seconds: float = 0.7
    timeout_seconds: float = 15.0
    reset_seconds: float = 1.0
    in_command: bool = False
    timed_out: bool = False
    before_command_speech_threshold: float = 0.2
    in_command_speech_threshold: float = 0.5

    _speech_seconds_left: float = field(default=0.0, repr=False)
    _command_seconds_left: float = field(default=0.0, repr=False)
    _silence_seconds_left: float = field(default=0.0, repr=False)
    _timeout_seconds_left: float = field(default=0.0, repr=False)
    _reset_seconds_left: float = field(default=0.0, repr=False)

    def __post_init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._speech_seconds_left = self.speech_seconds
        self._command_seconds_left = self.command_seconds - self.speech_seconds
        self._silence_seconds_left = self.silence_seconds
        self._timeout_seconds_left = self.timeout_seconds
        self._reset_seconds_left = self.reset_seconds
        self.in_command = False

    def process(self, chunk_seconds: float, speech_probability: float | None) -> bool:
        """Return False when the command is done."""
        if self.timed_out:
            self.timed_out = False

        self._timeout_seconds_left -= chunk_seconds
        if self._timeout_seconds_left <= 0:
            self.reset()
            self.timed_out = True
            return False

        if speech_probability is None:
            speech_probability = 0.0

        if not self.in_command:
            if speech_probability > self.before_command_speech_threshold:
                self._reset_seconds_left = self.reset_seconds
                self._speech_seconds_left -= chunk_seconds
                if self._speech_seconds_left <= 0:
                    self.in_command = True
                    self._command_seconds_left = (
                        self.command_seconds - self.speech_seconds
                    )
                    self._silence_seconds_left = self.silence_seconds
            else:
                self._reset_seconds_left -= chunk_seconds
                if self._reset_seconds_left <= 0:
                    self._speech_seconds_left = self.speech_seconds
                    self._reset_seconds_left = self.reset_seconds
        else:
            if speech_probability > self.in_command_speech_threshold:
                self._reset_seconds_left -= chunk_seconds
                self._command_seconds_left -= chunk_seconds
                if self._reset_seconds_left <= 0:
                    self._silence_seconds_left = self.silence_seconds
                    self._reset_seconds_left = self.reset_seconds
            else:
                self._reset_seconds_left = self.reset_seconds
                self._silence_seconds_left -= chunk_seconds
                self._command_seconds_left -= chunk_seconds
                if self._silence_seconds_left <= 0 and self._command_seconds_left <= 0:
                    self.reset()
                    return False

        return True


@dataclass
class HaVerdict:
    cut_ms: int | None
    timed_out: bool
    clip_ms: int
    detector: str
    exact: bool

    @property
    def truncated(self) -> bool:
        """True if HA stopped listening while audio remained."""
        return self.cut_ms is not None and self.cut_ms < self.clip_ms

    @property
    def lost_ms(self) -> int:
        if not self.truncated or self.cut_ms is None:
            return 0
        return self.clip_ms - self.cut_ms


def ha_cut_point(clip: Clip, detector: SpeechDetector,
                 silence_seconds: float) -> HaVerdict:
    """Find where HA's pipeline would have stopped consuming this clip."""
    segmenter = VoiceCommandSegmenter(silence_seconds=silence_seconds)
    chunk_seconds = VAD_CHUNK_MS / 1000.0
    elapsed_ms = 0

    for chunk in _iter_chunks(clip.pcm, clip.rate, VAD_CHUNK_MS):
        if not segmenter.process(chunk_seconds, detector.probability(chunk)):
            return HaVerdict(
                cut_ms=elapsed_ms + VAD_CHUNK_MS,
                timed_out=segmenter.timed_out,
                clip_ms=int(clip.duration_s * 1000),
                detector=detector.name,
                exact=detector.exact,
            )
        elapsed_ms += VAD_CHUNK_MS

    return HaVerdict(
        cut_ms=None,
        timed_out=False,
        clip_ms=int(clip.duration_s * 1000),
        detector=detector.name,
        exact=detector.exact,
    )


# --------------------------------------------------------------------------
# The Muse side
# --------------------------------------------------------------------------


@dataclass
class MuseVerdict:
    transcript: str = ""
    speech_start_ms: int | None = None
    speech_end_ms: int | None = None
    complete_ms: int | None = None
    first_partial_wall_ms: int | None = None
    complete_wall_ms: int | None = None
    speakers: list[dict] = field(default_factory=list)
    turns: list[dict] = field(default_factory=list)
    error: str | None = None

    @property
    def endpoint_lag_ms(self) -> int | None:
        """Wall-clock delay between the audio ending and text arriving.

        This is the number a listener actually feels. HA's equivalent is its
        700ms silence timer plus whatever the STT round trip costs, so the
        two are directly comparable.
        """
        if self.complete_wall_ms is None or self.speech_end_ms is None:
            return None
        return self.complete_wall_ms - self.speech_end_ms


async def probe_muse(clip: Clip, api_key: str, mode: str,
                     keywords: list[str] | None = None,
                     languages: list[str] | None = None,
                     endpoint: str = MUSE_ENDPOINT) -> MuseVerdict:
    """Stream one clip to Muse Voice Transcribe at real-time pace.

    `endpoint` exists so the client can be exercised against a local mock
    without a paid API call or a working trust store, and so a run behind a
    TLS-intercepting proxy has somewhere to point.
    """
    import websockets  # noqa: PLC0415 - optional dependency

    verdict = MuseVerdict()
    encoding = "PCM_24KHZ" if clip.rate == 24000 else "PCM_16KHZ"
    handshake = {
        "authorization": {"accessToken": f"Bearer {api_key}"},
        "audioEncoding": encoding,
        "model": MUSE_MODEL,
        "mode": mode,
        "partialMode": "CUMULATIVE",
        "emitAudioProgress": False,
    }
    if keywords:
        handshake["keywords"] = keywords
    if languages:
        handshake["languageBias"] = languages

    frame_bytes = int(clip.rate * HA_SAMPLE_WIDTH * SEND_FRAME_MS / 1000)

    try:
        async with websockets.connect(endpoint, max_size=None) as ws:
            await ws.send(json.dumps(handshake))

            ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if "type" in ack:  # the ack is the only frame without one
                verdict.error = f"handshake rejected: {ack}"
                return verdict

            started = time.monotonic()

            async def receive() -> None:
                async for raw in ws:
                    if isinstance(raw, bytes):
                        continue
                    wall_ms = int((time.monotonic() - started) * 1000)
                    _apply_event(verdict, json.loads(raw), wall_ms)

            receiver = asyncio.create_task(receive())

            for offset in range(0, len(clip.pcm), frame_bytes):
                await ws.send(clip.pcm[offset : offset + frame_bytes])
                # Pace against the clock, not a fixed sleep, so scheduler
                # jitter cannot accumulate into below-real-time ingress.
                target = started + (offset + frame_bytes) / (
                    clip.rate * HA_SAMPLE_WIDTH
                )
                drift = target - time.monotonic()
                if drift > 0:
                    await asyncio.sleep(drift)

            await ws.send(json.dumps({"type": "endStream"}))
            try:
                await asyncio.wait_for(receiver, timeout=30)
            except TimeoutError:
                receiver.cancel()
                if not verdict.transcript:
                    verdict.error = "no final transcript before timeout"
    except Exception as err:  # noqa: BLE001 - a probe reports, never raises
        verdict.error = f"{type(err).__name__}: {err}"

    return verdict


def _apply_event(verdict: MuseVerdict, event: dict, wall_ms: int) -> None:
    """Fold one server event into the verdict."""
    kind = event.get("type")

    if kind == "transcript":
        if verdict.first_partial_wall_ms is None:
            verdict.first_partial_wall_ms = wall_ms
        text = event.get("transcript", "")
        if text:
            verdict.transcript = text
        if event.get("final"):
            verdict.complete_wall_ms = wall_ms
            verdict.complete_ms = event.get("audioProcessedMs")
            # PUSH_TO_TALK has no speechEnd; the final transcript is the
            # only endpoint signal there is, so treat it as one.
            if verdict.speech_end_ms is None:
                verdict.speech_end_ms = event.get("audioProcessedMs")

    elif kind == "speechStart":
        if verdict.speech_start_ms is None:
            verdict.speech_start_ms = event.get("audioProcessedMs")

    elif kind == "speechEnd":
        verdict.speech_end_ms = event.get("audioProcessedMs")

    elif kind == "speechComplete":
        verdict.transcript = event.get("transcript", verdict.transcript)
        verdict.complete_ms = event.get("audioProcessedMs")
        verdict.complete_wall_ms = wall_ms
        verdict.turns.append({
            "turn_id": event.get("turnId"),
            "transcript": event.get("transcript", ""),
            "audio_processed_ms": event.get("audioProcessedMs"),
        })

    elif kind == "speaker":
        verdict.speakers.append({
            "label": event.get("label"),
            "audio_processed_ms": event.get("audioProcessedMs"),
        })

    elif kind == "error":
        verdict.error = event.get("message", "unspecified server error")


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def _fmt_ms(value: int | None) -> str:
    return "--" if value is None else f"{value / 1000:.2f}s"


def render_report(rows: list[dict], ran_muse: bool, exact_vad: bool) -> str:
    out: list[str] = []

    if not exact_vad:
        out.append(
            "WARNING: pymicro-vad is not installed, so the HA column is an RMS\n"
            "approximation, not Home Assistant's actual decision. Treat the\n"
            "truncation counts as indicative. Install pymicro-vad (needs a C++\n"
            "toolchain) or run this on the HA instance for an exact answer.\n"
        )

    header = f"{'clip':<28} {'length':>8} {'HA cuts':>9} {'lost':>8}"
    if ran_muse:
        header += f" {'Muse end':>9} {'lag':>8}  transcript"
    out.append(header)
    out.append("-" * (len(header) + 20))

    for row in rows:
        ha = row["ha"]
        line = (
            f"{row['clip']:<28} {_fmt_ms(ha['clip_ms']):>8} "
            f"{_fmt_ms(ha['cut_ms']):>9} "
            f"{(_fmt_ms(ha['lost_ms']) if ha['truncated'] else '-'):>8}"
        )
        if ran_muse:
            muse = row.get("muse") or {}
            if muse.get("error"):
                line += f" {'ERROR':>9} {'--':>8}  {muse['error'][:60]}"
            else:
                line += (
                    f" {_fmt_ms(muse.get('speech_end_ms')):>9} "
                    f"{_fmt_ms(muse.get('endpoint_lag_ms')):>8}  "
                    f"{muse.get('transcript', '')[:60]}"
                )
        out.append(line)

    truncated = [r for r in rows if r["ha"]["truncated"]]
    timed_out = [r for r in rows if r["ha"]["timed_out"]]

    out.append("")
    out.append(f"Clips:                    {len(rows)}")
    out.append(f"HA would truncate:        {len(truncated)} of {len(rows)}")
    out.append(f"  of which hit the 15s cap: {len(timed_out)}")

    if truncated:
        lost = [r["ha"]["lost_ms"] for r in truncated]
        out.append(
            f"  audio lost, median:     {_fmt_ms(int(statistics.median(lost)))}"
            f"  (worst {_fmt_ms(max(lost))})"
        )

    if ran_muse:
        lags = [
            r["muse"]["endpoint_lag_ms"]
            for r in rows
            if r.get("muse") and r["muse"].get("endpoint_lag_ms") is not None
        ]
        if lags:
            out.append(
                f"Muse endpoint lag, median: {_fmt_ms(int(statistics.median(lags)))}"
                f"  (worst {_fmt_ms(max(lags))})"
            )
            out.append(
                "  HA's comparable figure is its 700ms silence timer plus the\n"
                "  STT round trip, so lag under ~700ms is a straight win."
            )

        rescued = [
            r for r in rows
            if r["ha"]["truncated"]
            and r.get("muse")
            and not r["muse"].get("error")
            and (r["muse"].get("speech_end_ms") or 0) > (r["ha"]["cut_ms"] or 0)
        ]
        out.append("")
        out.append(
            f"VERDICT: Muse kept listening past HA's cut on {len(rescued)} of "
            f"{len(truncated)} truncated clips."
        )
        if not truncated:
            out.append(
                "  No clip was truncated, so this run says nothing either way."
                "\n  Record utterances with mid-sentence pauses and try again."
            )

    return "\n".join(out)


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--clips", type=Path,
                    help="directory of WAV files, or a single .wav")
    ap.add_argument("--muse", action="store_true",
                    help="also stream each clip to Muse (needs META_API_KEY)")
    ap.add_argument("--mode", default="ENDPOINTING",
                    choices=["ENDPOINTING", "PUSH_TO_TALK", "DIARIZATION"],
                    help="Muse session mode (default: ENDPOINTING)")
    ap.add_argument("--vad-sensitivity", default="default",
                    choices=["aggressive", "default", "relaxed"],
                    help="which HA VAD sensitivity to model (default: default)")
    ap.add_argument("--rate", type=int, default=16000, choices=[16000, 24000],
                    help="sample rate to send to Muse (default: 16000)")
    ap.add_argument("--keywords", nargs="*",
                    help="vocabulary biasing, e.g. room and speaker names")
    ap.add_argument("--languages", nargs="*", help="language biasing")
    ap.add_argument("--endpoint", default=MUSE_ENDPOINT,
                    help="override the Muse websocket URL (testing, proxies)")
    ap.add_argument("--json", type=Path, help="write full results here")
    ap.add_argument("--help-recording", action="store_true",
                    help="explain how to capture representative audio")
    args = ap.parse_args(argv)

    if args.help_recording:
        print(RECORDING_HELP)
        return 0

    if args.clips is None:
        ap.error("--clips is required (or use --help-recording)")

    if args.clips.is_dir():
        paths = sorted(args.clips.glob("*.wav"))
    elif args.clips.is_file():
        paths = [args.clips]
    else:
        print(f"No such path: {args.clips}", file=sys.stderr)
        return 2

    if not paths:
        print(f"No .wav files in {args.clips}", file=sys.stderr)
        return 2

    api_key = os.environ.get("META_API_KEY", "").strip()
    if args.muse and not api_key:
        print(
            "META_API_KEY is not set. Get a key from https://dev.meta.ai/ and\n"
            "export it, or drop --muse to run the HA-side analysis alone.",
            file=sys.stderr,
        )
        return 2

    silence_seconds = {
        "aggressive": 0.25, "default": 0.7, "relaxed": 1.25,
    }[args.vad_sensitivity]

    rows: list[dict] = []
    exact_vad = True

    for path in paths:
        try:
            ha_clip = load_clip(path, HA_SAMPLE_RATE)
        except (ValueError, wave.Error) as err:
            print(f"Skipping {path.name}: {err}", file=sys.stderr)
            continue

        try:
            detector: SpeechDetector = MicroVadDetector()
        except ImportError:
            detector = RmsDetector(ha_clip.pcm)
            exact_vad = False

        verdict = ha_cut_point(ha_clip, detector, silence_seconds)
        row = {
            "clip": path.name,
            "ha": {
                "cut_ms": verdict.cut_ms,
                "clip_ms": verdict.clip_ms,
                "lost_ms": verdict.lost_ms,
                "truncated": verdict.truncated,
                "timed_out": verdict.timed_out,
                "detector": verdict.detector,
                "exact": verdict.exact,
                "silence_seconds": silence_seconds,
            },
        }

        if args.muse:
            muse_clip = (
                ha_clip if args.rate == HA_SAMPLE_RATE
                else load_clip(path, args.rate)
            )
            muse = asyncio.run(probe_muse(
                muse_clip, api_key, args.mode,
                keywords=args.keywords, languages=args.languages,
                endpoint=args.endpoint))
            row["muse"] = {
                "transcript": muse.transcript,
                "speech_start_ms": muse.speech_start_ms,
                "speech_end_ms": muse.speech_end_ms,
                "complete_ms": muse.complete_ms,
                "first_partial_wall_ms": muse.first_partial_wall_ms,
                "complete_wall_ms": muse.complete_wall_ms,
                "endpoint_lag_ms": muse.endpoint_lag_ms,
                "speakers": muse.speakers,
                "turns": muse.turns,
                "error": muse.error,
                "mode": args.mode,
            }

        rows.append(row)

    if not rows:
        print("No clips could be read.", file=sys.stderr)
        return 2

    print(render_report(rows, ran_muse=args.muse, exact_vad=exact_vad))

    if args.json:
        args.json.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"\nWrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
