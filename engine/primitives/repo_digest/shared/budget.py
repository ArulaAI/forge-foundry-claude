"""Token budget enforcement for repo digests."""

from __future__ import annotations

TOKEN_PER_WORD_RATIO = 1.3
_TRUNCATION_MARKER = "\n\n(...truncated to fit token budget...)\n"


class BudgetExceededError(RuntimeError):
    """Raised when an explicit budget assertion fails."""


def count_tokens(text: str) -> int:
    """Estimate tokens via word count x 1.3 proxy.

    Args:
        text: The text to count tokens for.

    Returns:
        Estimated token count (word count x 1.3).
    """
    word_count = len(text.split())
    return int(word_count * TOKEN_PER_WORD_RATIO)


def enforce_budget(text: str, max_tokens: int) -> str:
    """Truncate text to fit within max_tokens; preserves line boundaries.

    If the text fits within the budget, returns it unchanged.
    Otherwise, truncates from the end at line boundaries and appends
    a truncation marker.

    Args:
        text: The text to enforce budget on.
        max_tokens: Maximum token count allowed.

    Returns:
        Original text if it fits, or truncated text with marker appended.
    """
    if count_tokens(text) <= max_tokens:
        return text

    # Reserve room for marker. If the budget is smaller than the marker
    # itself, return empty string — better an empty result than one that
    # blows past the budget on its own (caught by hypothesis property test
    # in tests/test_budget_properties.py, #112 / MOD-6).
    marker_tokens = count_tokens(_TRUNCATION_MARKER)
    if max_tokens < marker_tokens:
        return ""
    target = max_tokens - marker_tokens

    lines = text.splitlines(keepends=True)
    running = 0
    kept: list[str] = []
    for line in lines:
        line_tokens = count_tokens(line)
        if running + line_tokens > target:
            break
        kept.append(line)
        running += line_tokens

    return "".join(kept) + _TRUNCATION_MARKER
