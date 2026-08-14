from __future__ import annotations

import unicodedata
import re


search_normalization_sensitive_pattern = re.compile(
    "["
    "\\u00c0-\\u024f"
    "\\u0300-\\u036f"
    "\\u0370-\\u052f"
    "\\u1ab0-\\u1aff"
    "\\u1dc0-\\u1dff"
    "\\u1e00-\\u1eff"
    "\\u20d0-\\u20ff"
    "\\ufb00-\\ufb4f"
    "\\ufe20-\\ufe2f"
    "\\uff00-\\uffef"
    "]+"
)


def _fold_sensitive_sequence(match: re.Match[str]) -> str:
    decomposed = unicodedata.normalize("NFKD", match.group(0))
    return "".join(character for character in decomposed if not unicodedata.category(character).startswith("M"))


def normalize_search_text(value: str) -> str:
    """Normalize text without applying costly decomposition to ordinary CJK/ASCII runs."""
    return search_normalization_sensitive_pattern.sub(_fold_sensitive_sequence, value.casefold())


def normalized_match_original_span(value: str, normalized_query: str) -> tuple[int, int] | None:
    """Map the first normalized match back to its original Python string offsets."""
    if not normalized_query:
        return None
    normalized_parts: list[str] = []
    spans: list[tuple[int, int]] = []
    normalized_length = 0
    for index, character in enumerate(value):
        folded = normalize_search_text(character)
        if not folded:
            continue
        normalized_parts.append(folded)
        spans.extend([(index, index + 1)] * len(folded))
        normalized_length += len(folded)
    if normalized_length == 0:
        return None
    normalized_value = "".join(normalized_parts)
    start = normalized_value.find(normalized_query)
    if start < 0:
        return None
    end = start + len(normalized_query)
    return spans[start][0], spans[end - 1][1]
