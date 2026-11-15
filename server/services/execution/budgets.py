"""Shared conservative estimates for bounded, cross-provider model context.

The estimate is deliberately labelled approximate. Provider usage remains the
source of truth; the margin avoids treating a tokenizer for another model as an
exact measurement. Truncation never changes the durable source payload.
"""

from __future__ import annotations

import json
import math
import os
from functools import lru_cache
from typing import Any

try:
    TOKEN_SAFETY_MARGIN = max(1.0, float(os.getenv("OPENPOKE_AGENT_TOKEN_SAFETY_MARGIN", "1.15")))
except ValueError:
    TOKEN_SAFETY_MARGIN = 1.15


@lru_cache(maxsize=1)
def _encoding():
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        # Works offline, including installations without the tokenizer cache.
        return None


def estimate_tokens(text: Any) -> int:
    """Return a conservative estimate, never a provider token guarantee."""
    value = str(text) if text is not None else ""
    if not value:
        return 0
    encoding = _encoding()
    if encoding:
        count = len(encoding.encode(value, disallowed_special=()))
    else:
        # Typical English is ~4 characters/token; use two ASCII characters and
        # one non-ASCII byte per token as a cautious offline approximation.
        ascii_count = sum(ord(character) < 128 for character in value)
        non_ascii_bytes = len(value.encode("utf-8")) - ascii_count
        count = math.ceil(ascii_count / 2) + non_ascii_bytes
    return math.ceil(count * TOKEN_SAFETY_MARGIN)


def truncate_text(text: Any, max_tokens: int) -> str:
    """Fit a text excerpt to the shared estimate without touching storage."""
    value = str(text) if text is not None else ""
    if max_tokens <= 0:
        return ""
    if estimate_tokens(value) <= max_tokens:
        return value
    marker = "\n[excerpt truncated; retrieve source for details]"
    if estimate_tokens(marker) > max_tokens:
        marker = ""
    low, high = 0, len(value)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(value[:middle] + marker) <= max_tokens:
            low = middle
        else:
            high = middle - 1
    return value[:low] + marker


def bounded_json(value: Any, max_tokens: int) -> str:
    """Serialize a bounded, valid JSON result with explicit excerpt semantics."""
    serialized = json.dumps(value, ensure_ascii=False, default=str)
    if estimate_tokens(serialized) <= max_tokens:
        return serialized
    shell = {"truncated": True, "excerpt": ""}
    if estimate_tokens(json.dumps(shell)) > max_tokens:
        return "null" if estimate_tokens("null") <= max_tokens else ""
    low, high = 0, len(serialized)
    result = json.dumps(shell)
    while low <= high:
        middle = (low + high) // 2
        candidate = json.dumps({"truncated": True, "excerpt": serialized[:middle]}, ensure_ascii=False)
        if estimate_tokens(candidate) <= max_tokens:
            result, low = candidate, middle + 1
        else:
            high = middle - 1
    return result
