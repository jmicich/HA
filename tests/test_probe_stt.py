"""Tests for the STT probe harness.

The point of these is narrow. The probe's job is to make a comparison that a
decision rests on, so the parts that must be right are:

  * the ported segmenter still matches Home Assistant's constants,
  * the cut point it computes is where HA would actually stop,
  * an approximate VAD is never reported as though it were exact, and
  * the API key never reaches an output file.

Everything touching Meta's API is exercised against recorded event shapes
rather than the network; the probe is a measurement tool and its tests should
not need a paid API call to run.
"""

from __future__ import annotations

import asyncio
import json
import random
import struct
import sys
import time
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from probe_stt import (  # noqa: E402
    HA_SAMPLE_RATE,
    VAD_CHUNK_MS,
    Clip,
    HaVerdict,
    MuseVerdict,
    RmsDetector,
    SpeechDetector,
    VoiceCommandSegmenter,
    _apply_event,
    _iter_chunks,
    _resample_linear,
    ha_cut_point,
    load_clip,
    main,
    probe_muse,
    render_report,
)


class ScriptedDetector(SpeechDetector):
    """Returns a caller-supplied probability per chunk, then silence."""

    exact = True
    name = "scripted"

    def __init__(self, probabilities: list[float]) -> None:
        self._probabilities = probabilities
        self._index = 0

    def probability(self, chunk: bytes) -> float:
        if self._index < len(self._probabilities):
            value = self._probabilities[self._index]
        else:
            value = 0.0
        self._index += 1
        return value


def silent_clip(seconds: float, rate: int = HA_SAMPLE_RATE) -> Clip:
    samples = int(rate * seconds)
    return Clip(path=Path("synthetic.wav"), pcm=b"\x00\x00" * samples, rate=rate)


def write_wav(path: Path, samples: list[int], rate: int, channels: int = 1) -> Path:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return path


# --------------------------------------------------------------------------
# The ported segmenter
# --------------------------------------------------------------------------


def test_segmenter_constants_match_home_assistant():
    """Pin the values the probe's whole argument rests on.

    If Home Assistant changes these, this test fails and the port gets
    re-read rather than silently measuring a pipeline that no longer exists.
    Source: assist_pipeline/vad.py at 2026.8.2.
    """
    segmenter = VoiceCommandSegmenter()
    assert segmenter.speech_seconds == 0.3
    assert segmenter.command_seconds == 1.0
    assert segmenter.silence_seconds == 0.7
    assert segmenter.timeout_seconds == 15.0
    assert segmenter.reset_seconds == 1.0
    assert segmenter.before_command_speech_threshold == 0.2
    assert segmenter.in_command_speech_threshold == 0.5


def test_speech_then_silence_cuts_after_the_silence_window():
    """0.3s to enter the command, then 0.7s of silence ends it."""
    speech_chunks = int(0.3 / (VAD_CHUNK_MS / 1000))
    detector = ScriptedDetector([1.0] * speech_chunks)

    verdict = ha_cut_point(silent_clip(5.0), detector, silence_seconds=0.7)

    assert verdict.cut_ms is not None
    # 0.3s of speech + 0.7s of silence, within one chunk of float drift.
    assert 980 <= verdict.cut_ms <= 1020
    assert not verdict.timed_out
    assert verdict.truncated
    assert verdict.lost_ms == verdict.clip_ms - verdict.cut_ms


def test_relaxed_sensitivity_buys_more_silence_but_not_much():
    """The knob a user can actually turn moves the cut by half a second."""
    speech_chunks = int(0.3 / (VAD_CHUNK_MS / 1000))

    default = ha_cut_point(
        silent_clip(5.0), ScriptedDetector([1.0] * speech_chunks), 0.7)
    relaxed = ha_cut_point(
        silent_clip(5.0), ScriptedDetector([1.0] * speech_chunks), 1.25)

    assert relaxed.cut_ms is not None and default.cut_ms is not None
    assert relaxed.cut_ms - default.cut_ms == pytest.approx(550, abs=20)


def test_continuous_speech_hits_the_fifteen_second_ceiling():
    """The cap nothing in HA's config can reach."""
    twenty_seconds = int(20 / (VAD_CHUNK_MS / 1000))
    detector = ScriptedDetector([1.0] * twenty_seconds)

    verdict = ha_cut_point(silent_clip(20.0), detector, silence_seconds=0.7)

    assert verdict.timed_out
    assert verdict.cut_ms is not None
    assert 14900 <= verdict.cut_ms <= 15100
    assert verdict.truncated


def test_a_clip_that_never_trips_the_vad_is_not_truncated():
    verdict = ha_cut_point(silent_clip(2.0), ScriptedDetector([]), 0.7)

    assert verdict.cut_ms is None
    assert not verdict.truncated
    assert verdict.lost_ms == 0


def test_mid_sentence_pause_shorter_than_the_window_survives():
    """A 0.5s hesitation must not end the turn; a 0.7s one must.

    This is the exact behaviour the whole probe exists to measure, so it is
    worth asserting directly rather than inferring from the cut point.
    """
    chunk_s = VAD_CHUNK_MS / 1000
    speech = [1.0] * int(0.5 / chunk_s)
    short_pause = [0.0] * int(0.5 / chunk_s)
    long_pause = [0.0] * int(0.8 / chunk_s)

    survives = ha_cut_point(
        silent_clip(5.0), ScriptedDetector(speech + short_pause + speech), 0.7)
    cut = ha_cut_point(
        silent_clip(5.0), ScriptedDetector(speech + long_pause + speech), 0.7)

    # The short pause is absorbed: the cut lands after the *second* burst.
    assert survives.cut_ms is not None and survives.cut_ms > 1500
    # The long one ends the turn before the second burst is ever reached.
    assert cut.cut_ms is not None and cut.cut_ms < 1500


# --------------------------------------------------------------------------
# Audio loading
# --------------------------------------------------------------------------


def test_stereo_is_averaged_not_half_discarded(tmp_path):
    """A satellite array mixed to stereo must not lose half its signal."""
    path = write_wav(
        tmp_path / "stereo.wav", [1000, 2000] * 100, HA_SAMPLE_RATE, channels=2)

    clip = load_clip(path, HA_SAMPLE_RATE)

    samples = struct.unpack(f"<{len(clip.pcm) // 2}h", clip.pcm)
    assert len(samples) == 100
    assert all(s == 1500 for s in samples)


def test_resampling_preserves_duration(tmp_path):
    path = write_wav(tmp_path / "hi.wav", [500] * 48000, 48000)

    clip = load_clip(path, HA_SAMPLE_RATE)

    assert clip.rate == HA_SAMPLE_RATE
    assert clip.duration_s == pytest.approx(1.0, abs=0.01)


def test_resample_is_a_no_op_at_matching_rates():
    pcm = struct.pack("<4h", 1, 2, 3, 4)
    assert _resample_linear(pcm, 16000, 16000) == pcm


def test_eight_bit_audio_is_refused_with_a_useful_message(tmp_path):
    path = tmp_path / "eight.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(1)
        wav.setframerate(HA_SAMPLE_RATE)
        wav.writeframes(b"\x80" * 100)

    with pytest.raises(ValueError, match="16-bit"):
        load_clip(path, HA_SAMPLE_RATE)


# --------------------------------------------------------------------------
# Muse event folding
# --------------------------------------------------------------------------


def test_endpointing_events_produce_a_lag_measurement():
    verdict = MuseVerdict()

    _apply_event(verdict, {"type": "speechStart", "turnId": 1,
                           "audioProcessedMs": 300}, wall_ms=320)
    _apply_event(verdict, {"type": "speechEnd", "turnId": 1,
                           "audioProcessedMs": 4200}, wall_ms=4260)
    _apply_event(verdict, {"type": "speechComplete", "turnId": 1,
                           "transcript": "play something quiet",
                           "audioProcessedMs": 4200}, wall_ms=4380)

    assert verdict.transcript == "play something quiet"
    assert verdict.speech_start_ms == 300
    assert verdict.speech_end_ms == 4200
    # 4380ms wall clock minus 4200ms of audio: the delay a listener feels.
    assert verdict.endpoint_lag_ms == 180
    assert len(verdict.turns) == 1


def test_push_to_talk_final_transcript_counts_as_an_endpoint():
    """PUSH_TO_TALK emits no speechEnd, so the final transcript must serve."""
    verdict = MuseVerdict()

    _apply_event(verdict, {"type": "transcript", "transcript": "partial",
                           "final": False, "audioProcessedMs": 1000},
                 wall_ms=1010)
    _apply_event(verdict, {"type": "transcript", "transcript": "the whole thing",
                           "final": True, "audioProcessedMs": 2500},
                 wall_ms=2600)

    assert verdict.transcript == "the whole thing"
    assert verdict.first_partial_wall_ms == 1010
    assert verdict.speech_end_ms == 2500
    assert verdict.endpoint_lag_ms == 100


def test_a_later_empty_partial_does_not_erase_the_transcript():
    verdict = MuseVerdict()

    _apply_event(verdict, {"type": "transcript", "transcript": "kitchen lights",
                           "final": False, "audioProcessedMs": 800}, wall_ms=810)
    _apply_event(verdict, {"type": "transcript", "transcript": "",
                           "final": False, "audioProcessedMs": 900}, wall_ms=910)

    assert verdict.transcript == "kitchen lights"


def test_diarization_speaker_spans_are_collected():
    verdict = MuseVerdict()

    _apply_event(verdict, {"type": "speaker", "label": "A",
                           "audioProcessedMs": 2480}, wall_ms=2500)
    _apply_event(verdict, {"type": "speaker", "label": "B",
                           "audioProcessedMs": 5100}, wall_ms=5150)

    assert [s["label"] for s in verdict.speakers] == ["A", "B"]


def test_server_error_is_captured_rather_than_raised():
    verdict = MuseVerdict()

    _apply_event(verdict, {"type": "error", "message": "rate limited"},
                 wall_ms=10)

    assert verdict.error == "rate limited"
    assert verdict.endpoint_lag_ms is None


# --------------------------------------------------------------------------
# Reporting honesty
# --------------------------------------------------------------------------


def _row(**ha) -> dict:
    base = {"cut_ms": 1000, "clip_ms": 4000, "lost_ms": 3000, "truncated": True,
            "timed_out": False, "detector": "scripted", "exact": True,
            "silence_seconds": 0.7}
    base.update(ha)
    return {"clip": "a.wav", "ha": base}


def test_an_approximate_vad_is_labelled_as_one():
    """The approximation must never be presented as HA's real decision."""
    report = render_report([_row()], ran_muse=False, exact_vad=False)

    assert "WARNING" in report
    assert "approximation" in report
    assert "not Home Assistant's actual decision" in report


def test_an_exact_vad_carries_no_warning():
    report = render_report([_row()], ran_muse=False, exact_vad=True)

    assert "WARNING" not in report


def test_a_run_with_no_truncation_says_it_proves_nothing():
    """A green run on easy audio is the failure mode this report guards."""
    rows = [_row(cut_ms=None, truncated=False, lost_ms=0)]
    rows[0]["muse"] = {"transcript": "hi", "speech_end_ms": 900,
                       "endpoint_lag_ms": 120, "error": None}

    report = render_report(rows, ran_muse=True, exact_vad=True)

    assert "says nothing either way" in report


def test_verdict_counts_only_clips_muse_actually_outlasted():
    rows = [
        _row(cut_ms=1000, clip_ms=5000, lost_ms=4000, truncated=True),
        _row(cut_ms=1000, clip_ms=5000, lost_ms=4000, truncated=True),
    ]
    rows[0]["clip"] = "rescued.wav"
    rows[0]["muse"] = {"transcript": "kept going", "speech_end_ms": 4800,
                       "endpoint_lag_ms": 150, "error": None}
    rows[1]["clip"] = "agreed.wav"
    rows[1]["muse"] = {"transcript": "stopped too", "speech_end_ms": 950,
                       "endpoint_lag_ms": 140, "error": None}

    report = render_report(rows, ran_muse=True, exact_vad=True)

    assert "on 1 of 2 truncated clips" in report


def test_a_muse_error_does_not_sink_the_whole_report():
    rows = [_row()]
    rows[0]["muse"] = {"error": "1013: rate limited", "transcript": ""}

    report = render_report(rows, ran_muse=True, exact_vad=True)

    assert "ERROR" in report
    assert "rate limited" in report


# --------------------------------------------------------------------------
# End to end, and the secret
# --------------------------------------------------------------------------


def test_offline_run_needs_no_api_key(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("META_API_KEY", raising=False)
    clips = tmp_path / "clips"
    clips.mkdir()
    write_wav(clips / "quiet.wav", [0] * HA_SAMPLE_RATE, HA_SAMPLE_RATE)

    assert main(["--clips", str(clips)]) == 0

    assert "HA would truncate" in capsys.readouterr().out


def test_muse_without_a_key_fails_before_reading_any_audio(tmp_path, capsys,
                                                           monkeypatch):
    monkeypatch.delenv("META_API_KEY", raising=False)
    clips = tmp_path / "clips"
    clips.mkdir()
    write_wav(clips / "a.wav", [0] * 100, HA_SAMPLE_RATE)

    assert main(["--clips", str(clips), "--muse"]) == 2

    assert "META_API_KEY is not set" in capsys.readouterr().err


def test_the_api_key_never_reaches_the_json_output(tmp_path, monkeypatch):
    """The output file is the thing most likely to be pasted somewhere."""
    secret = "sk-meta-do-not-leak-me"
    monkeypatch.setenv("META_API_KEY", secret)
    clips = tmp_path / "clips"
    clips.mkdir()
    write_wav(clips / "a.wav", [0] * HA_SAMPLE_RATE, HA_SAMPLE_RATE)
    out = tmp_path / "result.json"

    assert main(["--clips", str(clips), "--json", str(out)]) == 0

    assert secret not in out.read_text(encoding="utf-8")
    assert json.loads(out.read_text(encoding="utf-8"))[0]["clip"] == "a.wav"


def test_a_missing_clips_path_is_an_error_not_a_traceback(tmp_path, capsys):
    assert main(["--clips", str(tmp_path / "nope")]) == 2

    assert "No such path" in capsys.readouterr().err


def test_recording_help_explains_the_truncation_trap(capsys):
    assert main(["--help-recording"]) == 0

    out = capsys.readouterr().out
    assert "already truncated at HA's cut" in out
    assert "untruncated" in out


def test_rms_detector_separates_speech_from_silence():
    """The fallback must at least get loud-versus-quiet right."""
    loud = struct.pack("<160h", *([8000] * 160))
    quiet = struct.pack("<160h", *([0] * 160))
    detector = RmsDetector(quiet * 50 + loud * 50)

    assert detector.probability(loud) > 0.5
    assert detector.probability(quiet) == 0.0


def _noise(seconds: float, amplitude: int, seed: int = 1) -> bytes:
    rng = random.Random(seed)
    count = int(HA_SAMPLE_RATE * seconds)
    return struct.pack(
        f"<{count}h", *[int(rng.gauss(0, amplitude)) for _ in range(count)])


def test_a_mostly_speech_clip_is_not_chopped_into_fake_silences():
    """Regression: a percentile floor put the threshold inside speech.

    A dictation clip is ~94% speech, so a 20th-percentile "noise floor"
    landed above the median speech level and only 35% of frames read as
    speech — turning continuous talking into a run of invented pauses and
    reporting a 2.2s cut on nine seconds of uninterrupted speech.
    """
    pcm = _noise(0.3, 40) + _noise(9.0, 3000) + _noise(0.3, 40)

    detector = RmsDetector(pcm)
    frames = _iter_chunks(pcm, HA_SAMPLE_RATE, VAD_CHUNK_MS)
    speech = sum(1 for f in frames if detector.probability(f) > 0.5)

    assert speech / len(frames) > 0.9


def test_long_continuous_speech_reaches_the_ceiling_through_the_fallback():
    """The same defect, asserted end to end where it actually mattered."""
    pcm = _noise(0.3, 40) + _noise(19.0, 3000)
    clip = Clip(path=Path("dictation.wav"), pcm=pcm, rate=HA_SAMPLE_RATE)

    verdict = ha_cut_point(clip, RmsDetector(pcm), silence_seconds=0.7)

    assert verdict.timed_out
    assert verdict.cut_ms is not None and verdict.cut_ms > 14000


def test_an_all_silence_clip_yields_no_speech_at_all():
    """No bimodality and nothing loud: the split must not be invented."""
    pcm = _noise(3.0, 30)

    detector = RmsDetector(pcm)
    frames = _iter_chunks(pcm, HA_SAMPLE_RATE, VAD_CHUNK_MS)

    assert all(detector.probability(f) == 0.0 for f in frames)


def test_a_real_pause_is_still_found_in_a_mixed_clip():
    """The fix must not cost the detector its actual job."""
    pcm = _noise(1.2, 3000) + _noise(0.9, 40) + _noise(2.0, 3000)
    clip = Clip(path=Path("hesitation.wav"), pcm=pcm, rate=HA_SAMPLE_RATE)

    verdict = ha_cut_point(clip, RmsDetector(pcm), silence_seconds=0.7)

    assert not verdict.timed_out
    assert verdict.truncated
    # 1.2s of speech then 0.7s into the 0.9s pause.
    assert 1800 <= verdict.cut_ms <= 2100


def test_ha_verdict_reports_no_loss_when_it_never_cut():
    verdict = HaVerdict(cut_ms=None, timed_out=False, clip_ms=3000,
                        detector="scripted", exact=True)

    assert not verdict.truncated
    assert verdict.lost_ms == 0


# --------------------------------------------------------------------------
# The websocket client, against a local mock
# --------------------------------------------------------------------------
#
# The real endpoint cannot be reached from CI, and a paid API call has no
# place in a test suite. What *can* be verified without either is everything
# this repo actually wrote: the handshake it constructs, that it paces audio
# rather than dumping it, that it half-closes with endStream, and that it
# folds the documented event sequence into the right numbers.


def _mock_server_scenario(clip, mode, events, record):
    """Serve one Muse-shaped session on localhost and probe it."""
    serve = pytest.importorskip("websockets.asyncio.server").serve

    async def handler(websocket):
        record["handshake"] = json.loads(await websocket.recv())
        await websocket.send(json.dumps({"sessionId": "mock-session"}))
        record["audio_bytes"] = 0
        async for message in websocket:
            if isinstance(message, bytes):
                record["audio_bytes"] += len(message)
                continue
            if json.loads(message).get("type") == "endStream":
                record["saw_end_stream"] = True
                for event in events:
                    await websocket.send(json.dumps(event))
                return

    async def scenario():
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            return await probe_muse(
                clip, "mock-key", mode, endpoint=f"ws://127.0.0.1:{port}")

    return asyncio.run(scenario())


def test_client_completes_a_full_endpointing_session():
    clip = Clip(path=Path("m.wav"), pcm=b"\x00\x00" * 3200, rate=HA_SAMPLE_RATE)
    record: dict = {}

    verdict = _mock_server_scenario(
        clip, "ENDPOINTING",
        [
            {"type": "speechStart", "turnId": 1, "audioProcessedMs": 120},
            {"type": "speechEnd", "turnId": 1, "audioProcessedMs": 190},
            {"type": "speechComplete", "turnId": 1, "audioProcessedMs": 190,
             "transcript": "turn the kitchen light down a bit"},
        ],
        record,
    )

    assert verdict.error is None
    assert verdict.transcript == "turn the kitchen light down a bit"
    assert verdict.speech_start_ms == 120
    assert verdict.speech_end_ms == 190
    assert verdict.endpoint_lag_ms is not None


def test_handshake_matches_the_documented_schema():
    clip = Clip(path=Path("m.wav"), pcm=b"\x00\x00" * 1600, rate=HA_SAMPLE_RATE)
    record: dict = {}

    _mock_server_scenario(clip, "DIARIZATION", [
        {"type": "speechComplete", "turnId": 1, "audioProcessedMs": 100,
         "transcript": "ok"}], record)

    handshake = record["handshake"]
    assert handshake["authorization"] == {"accessToken": "Bearer mock-key"}
    assert handshake["audioEncoding"] == "PCM_16KHZ"
    assert handshake["model"] == "muse-voice-transcribe-1.0"
    assert handshake["mode"] == "DIARIZATION"
    assert record["saw_end_stream"] is True


def test_the_whole_clip_is_sent_and_half_closed():
    """Audio must arrive intact; a dropped tail would skew every measurement."""
    clip = Clip(path=Path("m.wav"), pcm=b"\x01\x00" * 4000, rate=HA_SAMPLE_RATE)
    record: dict = {}

    _mock_server_scenario(clip, "ENDPOINTING", [
        {"type": "speechComplete", "turnId": 1, "audioProcessedMs": 250,
         "transcript": "done"}], record)

    assert record["audio_bytes"] == len(clip.pcm)


def test_audio_is_paced_at_real_time_not_dumped():
    """Meta closes with 1008 on below-real-time ingress, and a flood would
    also make every latency number meaningless."""
    seconds = 0.5
    clip = Clip(path=Path("m.wav"),
                pcm=b"\x00\x00" * int(HA_SAMPLE_RATE * seconds),
                rate=HA_SAMPLE_RATE)
    record: dict = {}

    started = time.monotonic()
    _mock_server_scenario(clip, "ENDPOINTING", [
        {"type": "speechComplete", "turnId": 1, "audioProcessedMs": 500,
         "transcript": "paced"}], record)
    elapsed = time.monotonic() - started

    assert elapsed >= seconds * 0.8


def test_a_server_error_frame_is_surfaced_not_swallowed():
    clip = Clip(path=Path("m.wav"), pcm=b"\x00\x00" * 1600, rate=HA_SAMPLE_RATE)
    record: dict = {}

    verdict = _mock_server_scenario(clip, "ENDPOINTING", [
        {"type": "error", "message": "quota exceeded", "sessionId": "x"}], record)

    assert verdict.error == "quota exceeded"


def test_a_refused_handshake_is_reported_as_an_error():
    """The ack is the only frame without a type; anything else is a refusal."""
    clip = Clip(path=Path("m.wav"), pcm=b"\x00\x00" * 1600, rate=HA_SAMPLE_RATE)
    serve = pytest.importorskip("websockets.asyncio.server").serve

    async def handler(websocket):
        await websocket.recv()
        await websocket.send(json.dumps(
            {"type": "error", "message": "invalid access token"}))

    async def scenario():
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            return await probe_muse(
                clip, "bad-key", "ENDPOINTING",
                endpoint=f"ws://127.0.0.1:{port}")

    verdict = asyncio.run(scenario())

    assert verdict.error is not None
    assert "handshake rejected" in verdict.error


def test_an_unreachable_endpoint_reports_rather_than_raises():
    clip = Clip(path=Path("m.wav"), pcm=b"\x00\x00" * 160, rate=HA_SAMPLE_RATE)

    verdict = asyncio.run(probe_muse(
        clip, "k", "ENDPOINTING", endpoint="ws://127.0.0.1:1/nope"))

    assert verdict.error is not None
    assert verdict.transcript == ""
