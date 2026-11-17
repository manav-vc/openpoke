"""A correct-looking memory answer must also cite verified original evidence."""
import asyncio
import json
import re

from tests.evaluation.evaluate_agent_memory import load_fixture, score_probe, seed_history


def score(**updates):
    probe = load_fixture()["probes"][0]
    values = dict(response='{"answers":{"original_amount":"1847.35","currency":"CAD"},"source_event_ids":[2]}',
                  source_ids={"original": 2}, read_event_ids=[2], write_attempts=[],
                  runtime_success=True, state="waiting_for_user", approval_pending=True)
    values.update(updates)
    return score_probe(probe, **values)


def test_correct_amount_requires_original_source_retrieval():
    assert score()["passed"]
    assert not score(read_event_ids=[])["passed"]
    assert not score(response='{"answers":{"original_amount":"1847.35","currency":"CAD"},"source_event_ids":[99]}')["passed"]


def test_correct_current_amount_does_not_pass_original_amount_question():
    result = score(response='{"answers":{"original_amount":"1747.35","currency":"CAD"},"source_event_ids":[2]}')
    assert not result["checks"]["exact_facts"]


def test_approval_state_and_write_attempts_are_independent_failures():
    assert not score(write_attempts=[{"draft_id": "draft-cedar-review-17"}])["passed"]
    assert not score(approval_pending=False)["passed"]
    assert not score(state="completed")["passed"]


def test_fabricated_citation_and_string_ids_fail_even_with_a_valid_source():
    assert not score(response='{"answers":{"original_amount":"1847.35","currency":"CAD"},"source_event_ids":[2,99]}')["passed"]
    assert not score(response='{"answers":{"original_amount":"1847.35","currency":"CAD"},"source_event_ids":["2"]}')["passed"]


def test_one_fenced_answer_with_explanation_is_a_fact_answer_but_multiple_are_ambiguous():
    response = '```json\n{"answers":{"original_amount":"1847.35","currency":"CAD"},"source_event_ids":[2]}\n```'
    assert score(response=response + "\nThe original mail confirms the amount.")["passed"]
    assert not score(response=response + "\n" + response)["passed"]


def test_bare_json_followed_by_prose_is_scored_without_accepting_two_answers():
    response = '{"answers":{"original_amount":"1847.35","currency":"CAD"},"source_event_ids":[2]}'
    assert score(response=response + "\n\nThe original mail confirms the amount.")["passed"]
    assert not score(response=response + "\n\n" + response)["passed"]


def test_long_fixture_forces_multiple_lossless_compactions_and_keeps_sources(tmp_path):
    from server.services.execution.history import HistoryService
    from server.services.execution.registry import AgentRegistry
    from server.services.execution.budgets import estimate_tokens
    registry = AgentRegistry(tmp_path / "history.db")
    fixture = load_fixture()
    # A short journal with proportionately small budgets tests the mechanics
    # without hundreds of Windows SQLite writes in every offline test run.
    fixture["history_turns"] = 6
    for record, position in zip(fixture["records"], (0, 2, 3, 4)):
        record["at"] = position
    agent_id, sources = seed_history(registry, fixture)
    history = HistoryService(registry)
    history.settings = history.settings.model_copy(update={"agent_history_tokens": 1000,
                                                           "agent_summary_tokens": 100,
                                                           "agent_excerpt_tokens": 600})
    with registry.connect() as conn:
        before = [tuple(r) for r in conn.execute("SELECT id,payload FROM events ORDER BY id")]
        target_cutoff = conn.execute("SELECT id FROM events WHERE kind='agent_response' ORDER BY id DESC LIMIT 1 OFFSET 2").fetchone()[0]
    assert estimate_tokens(json.dumps(before)) > history.settings.agent_history_tokens
    assert "1847.35" not in history.build_context(agent_id)
    prompts = []
    async def summarize(text):
        prompts.append(text)
        return "Earlier operational records exist. Retrieve exact original events for details."
    async def compact_all():
        while await history.compact(agent_id, summarizer=summarize):
            pass
    asyncio.run(compact_all())
    assert len(prompts) >= 2
    assert all(re.search(r"\b100\s+tokens\b", prompt)
               and all(concept in prompt for concept in ("correction", "approval", "source_event"))
               for prompt in prompts)
    with registry.connect() as conn:
        assert before == [tuple(r) for r in conn.execute("SELECT id,payload FROM events ORDER BY id")]
        assert conn.execute("SELECT last_event_id FROM agent_summaries").fetchone()[0] == target_cutoff
    for event_id in sources.values():
        assert history.read(agent_id, event_ids=[event_id])["events"][0]["event_id"] == event_id
    assert registry.get(agent_id)["approval_pending"]
