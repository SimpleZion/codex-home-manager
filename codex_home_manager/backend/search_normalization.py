from __future__ import annotations

import unicodedata
def normalize_search_text(value: str) -> str:
    """Match the browser's full compatibility decomposition and mark folding."""
    if value.isascii():
        return value.lower()
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(
        character
        for character in decomposed.casefold()
        if not unicodedata.category(character).startswith("M")
    )


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
