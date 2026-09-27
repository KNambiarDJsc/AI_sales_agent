from voice.turn_taking.turn_taking import TurnEvent, TurnTaker, TurnTakerConfig

FRAME_MS = 20


def _config(**overrides) -> TurnTakerConfig:
    defaults = dict(speech_debounce_ms=40, min_speech_segment_ms=60, end_of_turn_ms=60, max_turn_ms=1000)
    defaults.update(overrides)
    return TurnTakerConfig(**defaults)


def test_speech_started_fires_after_debounce_not_before():
    taker = TurnTaker(_config())
    assert taker.feed(True, FRAME_MS) is None  # 20ms of speech: below 40ms debounce
    assert taker.feed(True, FRAME_MS) == TurnEvent.SPEECH_STARTED  # 40ms reached


def test_short_noise_blip_does_not_start_a_turn():
    taker = TurnTaker(_config(speech_debounce_ms=100))
    assert taker.feed(True, FRAME_MS) is None
    assert taker.feed(False, FRAME_MS) is None  # dropped before debounce threshold
    assert not taker.speech_in_progress


def test_turn_ends_after_sustained_silence_following_real_speech():
    taker = TurnTaker(_config())
    events = [taker.feed(True, FRAME_MS) for _ in range(3)]  # 60ms speech: passes min segment too
    assert TurnEvent.SPEECH_STARTED in events

    events = [taker.feed(False, FRAME_MS) for _ in range(3)]  # 60ms silence == end_of_turn_ms
    assert events[-1] == TurnEvent.TURN_ENDED


def test_max_turn_forces_end_even_without_silence():
    taker = TurnTaker(_config(max_turn_ms=200))
    events = [taker.feed(True, FRAME_MS) for _ in range(30)]  # 600ms of continuous speech
    assert TurnEvent.MAX_TURN_REACHED in events
    assert TurnEvent.TURN_ENDED not in events


def test_state_resets_after_turn_ended():
    taker = TurnTaker(_config())
    for _ in range(3):
        taker.feed(True, FRAME_MS)
    for _ in range(3):
        taker.feed(False, FRAME_MS)
    assert not taker.speech_in_progress
