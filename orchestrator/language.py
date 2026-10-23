"""Which language the customer is speaking, for scripts with language_policy
match_customer: Hindi (STT writes Hindi speech in Devanagari), Hinglish (Hindi in
English letters) or English.

A short "yes", "okay", "sure" or "hello" says nothing about the language — Hindi
speakers use those words all the time — so such a line keeps whatever language the
call was already in (the script's `language` until the customer clearly speaks one).
"""
from __future__ import annotations

import re
from collections.abc import Iterable

DEVANAGARI = re.compile(r"[ऀ-ॿ]")

# Common Hindi words as they come out of English-letter transcription — enough to
# tell Hinglish from English; an English sentence contains none of them.
ROMAN_HINDI_WORDS = frozenset(
    "haan haa han nahi nahin nai kya kyun kyon kaise kaisa kitna kitne kitni hai hain tha thi ho hoon hu "
    "mujhe mujhko humein hume hum aap aapka aapki mera meri mere hamara apna bhai ji theek thik achha acha "
    "accha bolo boliye bataiye batao karna karo karenge karunga chahiye chahte abhi baad kal parso subah "
    "shaam raat dopahar paisa paise rupay rupaye bhej bhejo bhejiye matlab samjha samajh dobara mat lekin "
    "aur zaroor zarur bilkul kuch koi sab bahut bohot wala wali baat phir fir yaar arre arey".split()
)

# Fewer English words than this ("yes", "okay sure") doesn't show a language change.
_MIN_ENGLISH_WORDS = 3

HINDI, HINGLISH, ENGLISH = "hi", "hinglish", "en"


def detect(text: str) -> str | None:
    """HINDI, HINGLISH or ENGLISH for one customer line, or None when it's too short
    to tell."""
    if DEVANAGARI.search(text):
        return HINDI
    words = re.findall(r"[a-z']+", text.lower())
    if set(words) & ROMAN_HINDI_WORDS:
        return HINGLISH
    if len(words) >= _MIN_ENGLISH_WORDS:
        return ENGLISH
    return None


def latest(lines: Iterable[str]) -> str | None:
    """The language of the most recent line that shows one (lines oldest first)."""
    for text in reversed(list(lines)):
        found = detect(text)
        if found:
            return found
    return None
