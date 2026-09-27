from voice.audio.processing import FrameChunker, outbound_frame_bytes


def test_outbound_frame_bytes_matches_20ms_at_8khz():
    assert outbound_frame_bytes(20, 8000) == 160


def test_chunker_buffers_partial_frames_across_pushes():
    chunker = FrameChunker(frame_bytes=160)
    frames = chunker.push(b"a" * 100)
    assert frames == []  # not enough for a full frame yet

    frames = chunker.push(b"b" * 100)
    assert len(frames) == 1
    assert len(frames[0]) == 160
    assert frames[0] == b"a" * 100 + b"b" * 60


def test_chunker_emits_multiple_frames_from_one_large_push():
    chunker = FrameChunker(frame_bytes=160)
    frames = chunker.push(b"x" * 500)
    assert [len(f) for f in frames] == [160, 160, 160]
    assert chunker.flush() == b"x" * 20  # 500 - 3*160 = 20 leftover


def test_flush_returns_none_when_nothing_buffered():
    chunker = FrameChunker(frame_bytes=160)
    chunker.push(b"y" * 160)
    assert chunker.flush() is None


def test_flush_returns_and_clears_remainder():
    chunker = FrameChunker(frame_bytes=160)
    chunker.push(b"z" * 50)
    remainder = chunker.flush()
    assert remainder == b"z" * 50
    assert chunker.flush() is None
