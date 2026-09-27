from voice.session.sentence_split import split_into_speech_chunks


def test_empty_text_returns_empty_list():
    assert split_into_speech_chunks("") == []
    assert split_into_speech_chunks("   ") == []


def test_single_sentence_is_a_single_chunk():
    assert split_into_speech_chunks("Are you interested in selling online?") == [
        "Are you interested in selling online?"
    ]


def test_text_without_punctuation_is_a_single_chunk():
    assert split_into_speech_chunks("Hello there") == ["Hello there"]


def test_multi_sentence_text_splits_on_boundaries():
    text = "Great, thanks for confirming. Let's talk about your product. Do you sell online already?"
    chunks = split_into_speech_chunks(text, min_chunk_chars=1)
    assert chunks == [
        "Great, thanks for confirming.",
        "Let's talk about your product.",
        "Do you sell online already?",
    ]
    assert "".join(chunks).replace(" ", "") == text.replace(" ", "")


def test_short_leading_fragment_is_merged_forward():
    chunks = split_into_speech_chunks("Ok. Let's continue with the next question about your business.")
    assert len(chunks) == 1
    assert chunks[0].startswith("Ok.")
