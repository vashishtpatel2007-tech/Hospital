"""Fuzzy name matching for Indian names spelled different ways (Laxmi ~ Lakshmi)."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

# Applied in order; collapses common romanisation variants.
_RULES = [
    ("ksh", "x"), ("ks", "x"), ("sh", "s"), ("ph", "f"), ("th", "t"), ("dh", "d"), ("bh", "b"),
    ("kh", "k"), ("gh", "g"), ("jh", "j"), ("ch", "c"), ("w", "v"), ("z", "j"), ("q", "k"),
    ("ee", "i"), ("oo", "u"), ("aa", "a"), ("ou", "u"), ("y", "i"),
]


def normalise(name: str) -> str:
    s = re.sub(r"[^a-z]", "", name.lower())
    for a, b in _RULES:
        s = s.replace(a, b)
    s = re.sub(r"(.)\1+", r"\1", s)  # double letters -> single
    s = re.sub(r"(?<=.)[aeiou]$", "", s)  # drop a trailing vowel (Basavaraju ~ Basavaraj)
    return s


def name_score(query: str, given: str, family: str) -> float:
    """0..1 similarity between what the caller said and a stored name."""
    q = normalise(query)
    if not q:
        return 0.0
    full = normalise(given + family)
    g = normalise(given)
    scores = [SequenceMatcher(None, q, full).ratio(), SequenceMatcher(None, q, g).ratio()]
    if len(query.split()) == 1:  # one word: maybe just the given or family name
        scores.append(SequenceMatcher(None, q, normalise(family)).ratio())
    return max(scores)
