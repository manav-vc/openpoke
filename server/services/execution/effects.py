"""Explicit replay-risk classification for execution tools.

``read`` means no non-idempotent external action: it includes the runtime's
replay-safe local state/reference bookkeeping. New tools stay unknown until
their effect has been reviewed; their names or task instructions cannot make
them safe to repeat.
"""
from __future__ import annotations

from typing import Literal


READ_ONLY_TOOLS = frozenset({
    "task_email_search",
    "gmail_get_contacts", "gmail_get_people", "gmail_list_drafts", "gmail_search_people",
    # These read tools also appear in saved source records from earlier runtimes.
    "gmail_fetch_emails", "gmail_get_message",
    "listTriggers", "read_agent_history",
    "report_task_state", "confirm_task_reference",
})

MUTATING_TOOLS = frozenset({
    "gmail_create_draft", "gmail_execute_draft", "gmail_delete_draft",
    "gmail_forward_email", "gmail_reply_to_thread",
    "createTrigger", "updateTrigger",
})


def tool_effect(name: str) -> Literal["read", "write", "unknown"]:
    """Return an audited effect, conservatively retaining unclassified tools."""
    if name in READ_ONLY_TOOLS:
        return "read"
    if name in MUTATING_TOOLS:
        return "write"
    return "unknown"
