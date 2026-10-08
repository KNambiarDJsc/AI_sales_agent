"""Local speech backends (Kokoro TTS, Moonshine STT). The text-splitting tests always
run; the round-trip test runs the real models and is skipped where they aren't
installed (e.g. CI without the local backup)."""
import asyncio
import importlib.util

import numpy as np
import pytest

from speech.tts.kokoro_local import model_paths, split_for_tts


@pytest.mark.parametrize(
    "text,first",
    [
        # a one-word tail ("Naman.") isn't worth its own piece — the sentence is short anyway
        ("That's great to hear, Naman. What products would you like to sell?", "That's great to hear, Naman."),
        ("Thanks for confirming, Demo User from Demo Business. Is now a good time?", "Thanks for confirming,"),
        ("We can help you sell your products on Amazon and other online marketplaces.", "We can help you sell your products"),
        ("Hi there.", "Hi there."),
        ("Is now an okay time for a quick call?", "Is now an okay time for a quick call?"),  # short: untouched
    ],
)
def test_first_tts_piece_is_short(text, first):
    pieces = split_for_tts(text)
    assert pieces[0] == first
    assert " ".join(pieces).split() == text.split()  # nothing lost or reordered


def test_split_handles_empty_text():
    assert split_for_tts("   ") == []


_kokoro_ready = all(p.exists() for p in model_paths()) and importlib.util.find_spec("kokoro_onnx") is not None
_moonshine_ready = importlib.util.find_spec("moonshine_onnx") is not None


@pytest.mark.skipif(not (_kokoro_ready and _moonshine_ready), reason="local Kokoro/Moonshine not installed")
def test_kokoro_to_moonshine_round_trip():
    """Kokoro speaks a sentence, Moonshine hears it back — proves both local models
    load, produce/consume the formats the rest of the app uses (24 kHz and 16 kHz
    PCM16), and actually understand each other."""
    from speech.stt.moonshine_local import MoonshineSTTProvider
    from speech.tts.kokoro_local import KokoroTTSProvider
    from voice.audio.processing import ResampleState, resample_pcm16

    async def run():
        tts = KokoroTTSProvider()
        audio = b"".join([chunk async for chunk in tts.synthesize_stream("Yes, I am interested in selling on Amazon.")])
        assert len(audio) > 24000 * 2  # more than a second of 24 kHz PCM16
        assert np.abs(np.frombuffer(audio, dtype="<i2")).max() > 1000  # not silence
        stt_stream = await MoonshineSTTProvider().start_stream()
        await stt_stream.send_audio(resample_pcm16(audio, 24000, 16000, ResampleState()))
        result = await stt_stream.receive_final()
        return result.text.lower()

    text = asyncio.run(run())
    for word in ("interested", "selling", "amazon"):
        assert word in text, text


def test_trim_silence_keeps_the_speech_and_a_margin():
    from speech.stt.moonshine_local import trim_silence

    rate = 16000
    speech = 0.3 * np.sin(np.linspace(0, 2000, rate))  # 1 s of "speech"
    audio = np.concatenate([np.zeros(5 * rate), speech, np.zeros(2 * rate)]).astype(np.float32)
    trimmed = trim_silence(audio)
    assert 1.4 * rate <= len(trimmed) <= 1.8 * rate  # the speech plus ~0.3 s each side
    assert trim_silence(np.zeros(rate, dtype=np.float32)).size == rate  # all quiet: left alone


@pytest.mark.skipif(not (_kokoro_ready and _moonshine_ready), reason="local Kokoro/Moonshine not installed")
def test_phone_turn_with_seconds_of_silence_before_the_answer_is_still_heard():
    """Phone path: the STT buffer holds the silence while the customer listened to the
    agent. Moonshine returned "" for a clip with ~5 s of lead-in (caught in a simulated
    Vobiz call — the customer's first answer vanished)."""
    from speech.stt.moonshine_local import MoonshineSTTProvider
    from speech.tts.kokoro_local import KokoroTTSProvider
    from voice.audio.processing import ResampleState, resample_pcm16

    async def run():
        tts = KokoroTTSProvider()
        audio = b"".join([chunk async for chunk in tts.synthesize_stream("Yes, this is Naman from Test Business.")])
        speech_16k = resample_pcm16(audio, 24000, 16000, ResampleState())
        stt_stream = await MoonshineSTTProvider().start_stream()
        await stt_stream.send_audio(b"\x00\x00" * 16000 * 5 + speech_16k + b"\x00\x00" * 8000)
        result = await stt_stream.receive_final()
        return (result.text if result else "").lower()

    text = asyncio.run(run())
    assert "naman" in text or "test business" in text, text
