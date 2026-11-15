"""Bounded interaction context; the saved roster is deliberately not embedded."""

import json
from html import escape
from pathlib import Path
from typing import Dict, List, Optional

from ...config import get_settings
from ...services.execution.budgets import truncate_text

SYSTEM_PROMPT = (Path(__file__).parent / "system_prompt.md").read_text(encoding="utf-8").strip()


def build_system_prompt() -> str:
    return SYSTEM_PROMPT


def prepare_message_with_history(latest_text: str, transcript: str, message_type: str = "user",
                                 task_links: Optional[list] = None) -> List[Dict[str, str]]:
    history = truncate_text(transcript, get_settings().agent_interaction_history_tokens)
    tag = "new_agent_message" if message_type == "agent" else "new_user_message"
    content = "\n\n".join([
        f"<conversation_history>{escape(history or 'None', quote=False)}</conversation_history>",
        "<task_references>" + escape(json.dumps(task_links or [], default=str), quote=False) + "</task_references>",
        f"<{tag}>{escape(latest_text.strip(), quote=False)}</{tag}>",
    ])
    return [{"role": "user", "content": content}]
