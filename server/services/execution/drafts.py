"""Present pending drafts from successful execution-tool source records."""
from __future__ import annotations


def pending_draft_references(registry, agent_ids, *, request_id=None, generation=None, limit=10):
    ids = list(dict.fromkeys(agent_ids))
    if not ids:
        return []
    generation = registry.generation if generation is None else generation
    placeholders = ",".join("?" for _ in ids)
    query = f"""SELECT p.agent_id,p.draft_id,p.source_event_id FROM task_approvals p
        JOIN events e ON e.id=p.source_event_id AND e.agent_id=p.agent_id
        WHERE p.agent_id IN ({placeholders}) AND e.generation=? AND e.kind='tool_result'
        AND json_extract(e.payload,'$.tool')='gmail_create_draft'
        AND json_extract(e.payload,'$.success')=1
        AND p.draft_id NOT LIKE 'unknown-source:%'"""
    parameters = [*ids, generation]
    if request_id is not None:
        query += " AND e.request_id=?"
        parameters.append(request_id)
    query += " ORDER BY e.id DESC,p.draft_id LIMIT ?"
    parameters.append(max(1, min(limit, 20)))
    with registry.connect(generation=generation) as conn:
        return [dict(row) for row in conn.execute(query, parameters)]


def _draft_ids(result):
    found = set()

    def observe(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"draft_id", "draftId"} and isinstance(child, str):
                    found.add(child)
                if isinstance(child, (dict, list)):
                    observe(child)
        elif isinstance(value, list):
            for child in value:
                observe(child)

    observe(result)
    if isinstance(result, dict):
        data = result.get("data", result)
        if isinstance(data, dict) and isinstance(data.get("id"), str):
            found.add(data["id"])
    return found


def load_pending_draft(registry, agent_id, draft_id, *, generation=None, account_scope=None):
    """Load exact submitted content; model-generated preview text is never trusted."""
    generation = registry.generation if generation is None else generation
    agent = registry.get(agent_id)
    scopes = {r.get("scope") for r in agent["references"] if r.get("scope")}
    if account_scope and scopes and account_scope not in scopes:
        raise ValueError("Draft belongs to a different connected account")
    with registry.connect(generation=generation) as conn:
        source = conn.execute(
            "SELECT source_event_id FROM task_approvals WHERE agent_id=? AND draft_id=?",
            (agent_id, draft_id)).fetchone()
    if not source:
        raise ValueError("No pending Execution Agent draft matches these IDs. Submit drafting work to the agent first.")
    event = registry.get_event(source["source_event_id"])
    if event["kind"] != "tool_result" or event["agent_id"] != agent_id or event["generation"] != generation:
        raise ValueError("Draft source does not belong to this task and generation")
    payload = event["payload"]
    if isinstance(payload, dict) and payload.get("artifact_id"):
        payload = registry.get_artifact(payload["artifact_id"], agent_id)
    if (not isinstance(payload, dict) or payload.get("tool") != "gmail_create_draft"
            or payload.get("success") is not True):
        raise ValueError("Draft presentation requires successful execution-tool evidence")
    result = payload.get("result")
    if (isinstance(result, dict) and (result.get("error") or result.get("successful") is False
                                      or result.get("status") == "error")):
        raise ValueError("Draft creation did not succeed")
    if draft_id not in _draft_ids(result):
        raise ValueError("Draft ID does not match its execution-tool result")
    arguments = payload.get("arguments")
    if not isinstance(arguments, dict):
        raise ValueError("Original draft content is unavailable; inspect the source before continuing")
    content = {key: arguments.get(key) for key in ("recipient_email", "subject", "body")}
    if not all(isinstance(value, str) and value.strip() for value in content.values()):
        raise ValueError("Original draft recipient, subject, or body is unavailable")
    return dict(agent_id=agent_id, draft_id=draft_id, source_event_id=event["id"], **content)
