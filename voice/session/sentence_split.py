"""Splits agent speech into sentence-ish chunks for pipelined TTS synthesis
(`voice/session/session.py`): start synthesizing/sending sentence 1 while sentence 2 is
still being generated, instead of treating the whole multi-sentence response as one
opaque synthesis request. A single-sentence response produces a single chunk, so this
is a no-op (identical behavior to the unsplit path) for the common short-response case.
"""
from __future__ import annotations

import re

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


def split_into_speech_chunks(text: str, min_chunk_chars: int = 20) -> list[str]:
    """Never returns an empty list for non-empty input, and never returns a chunk
    shorter than `min_chunk_chars` unless the whole input is that short — a lone "Ok."
    or "Yes." gets merged into the neighboring sentence rather than firing off its own
    synthesis request for two words."""
    text = text.strip()
    if not text:
        return []

    parts = [p.strip() for p in _SENTENCE_BOUNDARY.split(text) if p.strip()]
    if len(parts) <= 1:
        return parts or [text]

    merged: list[str] = []
    for part in parts:
        if merged and len(merged[-1]) < min_chunk_chars:
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    return merged
