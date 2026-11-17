"""Shared outcome contract must not advantage either implementation's storage."""
from types import SimpleNamespace
import sqlite3

import pytest

from tests.evaluation.compare_agent_workflows import (
    ActionStore, BASELINE, common_score, describe_source, main, observe_conversation,
    summarize_common, pin_models,
)


def example(**checks):
    return dict(checks=dict(correct_owner=True, waiting_for_approval=False,
                            no_unapproved_send_attempt=True, **checks), model_calls=[
        dict(kind="embedding", usage=dict(prompt_tokens=17)),
        dict(kind="chat", usage=dict(prompt_tokens=120)),
        dict(kind="chat", usage=dict(prompt_tokens=300)),
    ], wrong_owner=False, duplicate_agents=0)


def test_native_approval_metadata_is_not_required_but_sends_still_fail():
    row = example()
    assert common_score(row)["passed"]
    row["checks"]["no_unapproved_send_attempt"] = False
    assert not common_score(row)["passed"]
    row["checks"]["no_unapproved_send_attempt"] = True
    row["checks"]["correct_owner"] = False
    assert not common_score(row)["passed"]


def test_comparison_counts_all_chat_prompt_usage_and_excludes_embeddings():
    common = common_score(example())
    assert common["prompt_tokens"] == 420
    assert common["chat_calls"] == 2


def test_draft_preview_is_reported_separately_from_requested_task_success():
    row = example(draft_presented_verbatim=False)
    common = common_score(row)
    assert common["passed"] and common["draft_preview_exact"] is False
    assert row["checks"]["draft_presented_verbatim"] is False
    summary = summarize_common([dict(common=common, elapsed_ms=10)], 1)
    assert summary["passed"] == 1
    assert summary["exact_draft_previews"] == 0 and summary["draft_preview_cases"] == 1
    row["checks"]["required_body_facts"] = False
    assert not common_score(row)["passed"]


def test_partial_or_failed_run_cannot_be_reported_as_complete_success():
    row = dict(common=common_score(example(required_body_facts=False)), elapsed_ms=20)
    summary = summarize_common([row], 10)
    assert summary["complete"] is False
    assert summary["passed"] == 0
    assert summary["evaluated_turns"] == 1


def test_historical_hardcoded_defaults_cannot_change_the_compared_model():
    settings = SimpleNamespace(interaction_agent_model="old", execution_agent_model="old")
    pin_models(settings, "same-model")
    assert settings.interaction_agent_model == settings.execution_agent_model == "same-model"


def test_each_arm_identifies_its_actual_application_source(tmp_path):
    server = tmp_path / "server"
    server.mkdir()
    source_file = server / "config.py"
    source_file.write_text("historical settings", encoding="utf-8")
    fork_source = dict(git_commit="fork-base", working_tree=True, application_sha256="fork-hash")
    upstream = describe_source(tmp_path, "upstream", fork_source)
    assert upstream["git_commit"] == BASELINE and upstream["working_tree"] is False
    source_file.write_text("current settings", encoding="utf-8")
    fork = describe_source(tmp_path, "fork", fork_source)
    assert fork["git_commit"] == "fork-base" and fork["working_tree"] is True
    assert upstream["application_sha256"] != fork["application_sha256"]
    assert fork_source["application_sha256"] == "fork-hash"


def test_action_transactions_commit_and_release_windows_handles(tmp_path):
    store = ActionStore(tmp_path / "actions.sqlite")
    with store.connect() as connection:
        connection.execute("CREATE TABLE actions(value TEXT)")
        connection.execute("INSERT INTO actions VALUES ('draft')")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT * FROM actions")
    with store.connect() as reopened:
        assert reopened.execute("SELECT value FROM actions").fetchone()[0] == "draft"


def test_multiline_draft_previews_survive_native_entry_observation():
    draft = "To: sarah@acme.example\nSubject: INV-1042\n\nHi Sarah,\n\nCAD 420 is due."
    source = [("poke_reply", "earlier", "Old turn"),
              ("user_message", "now", "Draft a reminder"),
              ("poke_reply", "now", draft),
              ("poke_reply", "now", "Ready for review")]
    log = SimpleNamespace(iter_entries=lambda: iter(source))
    entries, reply = observe_conversation(log, 1)
    assert reply == draft + "\nReady for review"
    assert entries[1]["content"] == draft
    assert "Old turn" not in reply


@pytest.mark.parametrize("budget", ["nan", "inf", "-inf"])
def test_nonfinite_budgets_are_rejected_before_live_calls(monkeypatch, budget):
    monkeypatch.setattr("sys.argv", ["compare_agent_workflows.py", "--max-cost-usd=" + budget])
    with pytest.raises(SystemExit) as stopped:
        main()
    assert stopped.value.code == 2
