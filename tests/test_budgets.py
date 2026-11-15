"""Check token limits, safe text truncation, and valid JSON excerpts."""
import json

from server.services.execution.budgets import bounded_json, estimate_tokens, truncate_text


def test_text_excerpt_is_bounded_and_original_is_unchanged():
    original = "Invoice INV-1042 remains unpaid. " * 1000
    result = truncate_text(original, 80)
    assert estimate_tokens(result) <= 80
    assert "truncated" in result
    assert len(original) > len(result)


def test_json_excerpt_remains_valid_with_escaping_and_unicode():
    value = {"text": 'A "quoted" amount \\ path 雪' * 2000}
    result = bounded_json(value, 100)
    assert estimate_tokens(result) <= 100
    assert json.loads(result)["truncated"] is True


def test_small_values_preserve_structure_and_empty_budgets_are_safe():
    assert json.loads(bounded_json({"ok": True}, 100)) == {"ok": True}
    assert truncate_text("hello", 0) == ""
    assert estimate_tokens("") == 0
    assert bounded_json({"long": "x" * 100}, 0) == ""
