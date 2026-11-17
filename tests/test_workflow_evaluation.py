"""Regression checks for honest workflow scoring and bounded live evaluation."""
import json
import os
from pathlib import Path

import pytest

from tests.evaluation.evaluate_agent_workflows import load_fixture, score_turn, summarize, snapshot
from tests.evaluation.live_provider import (
    BudgetExceeded, CatalogUnavailable, UsageLedger, LiveProvider, text_request_pricing, build_chat_payload,
)


def test_report_retries_temporary_windows_file_lock_without_losing_previous_result(tmp_path, monkeypatch):
    from tests.evaluation import evaluate_agent_workflows as evaluator
    destination = tmp_path / "report.json"
    destination.write_text('{"status":"previous"}', encoding="utf-8")
    original_replace = Path.replace
    attempts, pauses = [], []

    def locked_twice(source, target):
        attempts.append(source)
        if len(attempts) < 3:
            assert json.loads(destination.read_text())["status"] == "previous"
            raise PermissionError("simulated Windows sharing lock")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", locked_twice)
    monkeypatch.setattr(evaluator.time, "sleep", pauses.append)
    assert evaluator.write_json(destination, {"status": "updated"}) == destination
    assert json.loads(destination.read_text())["status"] == "updated"
    assert len(attempts) == 3 and len(pauses) == 2
    assert not list(tmp_path.glob("*.tmp"))


def test_permanent_report_lock_preserves_old_report_and_new_snapshot(tmp_path, monkeypatch):
    from tests.evaluation import evaluate_agent_workflows as evaluator
    destination = tmp_path / "report.json"
    destination.write_text('{"status":"previous"}', encoding="utf-8")

    def locked(source, target):
        raise PermissionError("persistent lock")

    monkeypatch.setattr(Path, "replace", locked)
    monkeypatch.setattr(evaluator.time, "sleep", lambda delay: None)
    with pytest.raises(PermissionError, match="complete new report is preserved"):
        evaluator.write_json(destination, {"status": "updated", "turns": [1]})
    assert json.loads(destination.read_text())["status"] == "previous"
    snapshots = list(tmp_path.glob("report.json.*.tmp"))
    assert len(snapshots) == 1
    assert json.loads(snapshots[0].read_text()) == {"status": "updated", "turns": [1]}


def test_report_writes_use_distinct_temporary_files(tmp_path, monkeypatch):
    from tests.evaluation import evaluate_agent_workflows as evaluator
    paths = []
    original_replace = Path.replace

    def record(source, target):
        paths.append(source)
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", record)
    for number in range(3):
        evaluator.write_json(tmp_path / "report.json", {"number": number})
    assert len(set(paths)) == 3
    assert json.loads((tmp_path / "report.json").read_text())["number"] == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows file-sharing regression")
def test_report_recovers_after_a_real_windows_reader_releases_its_lock(tmp_path):
    import ctypes
    from ctypes import wintypes
    import threading
    from tests.evaluation import evaluate_agent_workflows as evaluator

    destination = tmp_path / "report.json"
    destination.write_text('{"status":"previous"}', encoding="utf-8")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel.CreateFileW
    create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create_file.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    # Allow readers/writers but deny deletion/rename until the timer releases it.
    handle = create_file(str(destination), 0x80000000, 0x1 | 0x2, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    released = threading.Event()

    def release():
        kernel.CloseHandle(handle)
        released.set()

    timer = threading.Timer(.15, release)
    timer.start()
    try:
        evaluator.write_json(destination, {"status": "updated"})
    finally:
        timer.cancel()
        timer.join()
        if not released.is_set():
            kernel.CloseHandle(handle)
    assert json.loads(destination.read_text())["status"] == "updated"


def outcome(**updates):
    values = dict(
        expected=dict(expect="draft", owner="invoice", recipient="sarah@acme.example",
                      thread_id="thread-1042", body_terms=["1042", "420"]),
        jobs=[dict(agent_id="owner", status="succeeded")],
        actions=[dict(kind="draft", agent_id="owner", recipient_email="sarah@acme.example",
                      thread_id="thread-1042", subject="Payment reminder", body="Invoice INV-1042: CAD 420")],
        reply="To: sarah@acme.example\nSubject: Payment reminder\n\nInvoice INV-1042: CAD 420",
        agents_before={"owner": {}},
        agents_after={"owner": dict(state="waiting_for_user", approval_pending=1)},
        owner_ids={"invoice": "owner"},
    )
    values.update(updates)
    return score_turn(**values)


def test_claimed_success_without_a_persisted_draft_fails():
    assert outcome()["passed"]
    result = outcome(actions=[], reply="I successfully drafted exactly what you requested.")
    assert not result["passed"]
    assert not result["checks"]["exactly_one_persisted_draft"]


def test_correct_draft_from_wrong_owner_fails():
    result = outcome(jobs=[dict(agent_id="other", status="succeeded")])
    assert result["wrong_owner"] and not result["passed"]


def test_wrong_thread_or_recipient_or_facts_fail():
    for field, value in (("thread_id", "other-thread"), ("recipient_email", "other@example.test"),
                         ("body", "Invoice INV-1042: CAD 980")):
        action = dict(kind="draft", agent_id="owner", recipient_email="sarah@acme.example",
                      thread_id="thread-1042", body="Invoice INV-1042: CAD 420")
        action[field] = value
        assert not outcome(actions=[action])["passed"]


def test_extra_agents_and_repeated_drafts_fail():
    assert not outcome(agents_after={"owner": dict(state="waiting_for_user", approval_pending=1),
                                    "extra": {}})["passed"]
    action = dict(kind="draft", agent_id="owner", recipient_email="sarah@acme.example",
                  thread_id="thread-1042", body="Invoice INV-1042: CAD 420")
    assert not outcome(actions=[action, action])["passed"]


def test_unapproved_send_attempt_is_a_failure_even_when_blocked():
    result = outcome(actions=[dict(kind="blocked_send")])
    assert not result["checks"]["no_unapproved_send_attempt"]


def test_persisting_a_draft_without_showing_it_to_the_user_fails():
    assert not outcome(reply="Done, your draft is ready.")["passed"]


def test_reported_tool_failure_can_pass_without_a_successful_job():
    expected = dict(expect="tool_failure", owner="invoice",
                    reply_terms_any=["unavailable"], forbidden_reply_terms=["May 3"])
    values = dict(expected=expected, jobs=[dict(agent_id="owner", status="failed")],
                  actions=[dict(kind="read_error")], reply="The email service is unavailable.")
    assert outcome(**values)["passed"]
    values["reply"] = "The service is unavailable, but payment is due May 3."
    assert not outcome(**values)["passed"]


def test_clarification_requires_relevant_question_and_no_dispatch():
    expected = dict(expect="clarify", clarification_terms=[["which", "invoice"]])
    assert outcome(expected=expected, jobs=[], actions=[], reply="Which invoice do you mean?")["passed"]
    assert not outcome(expected=expected, jobs=[], actions=[], reply="How can I help?")["passed"]
    assert not outcome(expected=expected, actions=[], reply="Which invoice do you mean?")["passed"]


def test_read_answer_from_correct_saved_source_does_not_require_a_redundant_job():
    expected = dict(expect="read", owner="invoice", reply_terms=["420"])
    assert outcome(expected=expected, jobs=[], actions=[], reply="CAD 420",
                   history_owner_ids=["owner"])["passed"]
    assert not outcome(expected=expected, jobs=[], actions=[], reply="CAD 420")["passed"]


def test_partial_run_is_never_reported_as_complete_success():
    summary = summarize([dict(passed=True, expected="draft", wrong_owner=False,
                              duplicate_agents=0, elapsed_ms=25)], 10)
    assert summary["pass_rate"] == 1
    assert summary["coverage"] == .1
    assert not summary["all_passed"] and not summary["complete"]
    assert summarize([], 10)["pass_rate"] is None


def test_failed_unknown_usage_keeps_reservation_and_stops_further_spending():
    ledger = UsageLedger(max_calls=2, max_tokens=100, max_cost_usd=1)
    price = dict(prompt=.01, completion=.01, request=0)
    row = ledger.reserve(kind="chat", model="model", input_tokens=40, output_tokens=20,
                         pricing=price, case_id="case")
    ledger.finish(row, None, 1, error="ConnectError")
    assert ledger.summary()["provider_reported_cost_usd"] is None
    assert ledger.summary()["accounted_cost_usd"] == pytest.approx(.6)
    with pytest.raises(BudgetExceeded):
        ledger.reserve(kind="chat", model="model", input_tokens=40, output_tokens=20,
                       pricing=price, case_id="case")
    assert len(ledger.calls) == 1


def test_reported_usage_replaces_reservation_without_inventing_missing_cost():
    ledger = UsageLedger()
    row = ledger.reserve(kind="embedding", model="model", input_tokens=500, output_tokens=0,
                         pricing=dict(prompt=.001, completion=0, request=0), case_id="seed")
    ledger.finish(row, dict(total_tokens=30, prompt_tokens=30, cost=.03), 3)
    assert ledger.summary()["accounted_tokens"] == 30
    assert ledger.summary()["provider_reported_cost_usd"] == .03


def test_payment_required_stops_other_cases_without_repeated_network_calls(monkeypatch, tmp_path):
    import asyncio
    import httpx
    attempts = []
    async def post(client, url, **kwargs):
        attempts.append(url)
        return httpx.Response(402, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    ledger = UsageLedger()
    provider = LiveProvider("test-key", ledger, tmp_path / "calls.jsonl")
    provider.prices["model"] = dict(prompt=.000001, completion=.000001, request=0)
    async def evaluate():
        payload = dict(model="model", messages=[dict(role="user", content="Synthetic request")])
        with pytest.raises(httpx.HTTPStatusError):
            await provider._request("chat", "/chat/completions", payload)
        with pytest.raises(BudgetExceeded, match="HTTP 402"):
            await provider._request("chat", "/chat/completions", payload)
    asyncio.run(evaluate())
    assert len(attempts) == 1
    assert len(ledger.calls) == 1 and ledger.calls[0]["http_status"] == 402
    assert ledger.calls[0]["usage"] is None
    assert ledger.calls[0]["accounted_cost_usd"] == ledger.calls[0]["reserved_cost_usd"]


def test_catalog_model_validation_and_prices_without_network(monkeypatch, tmp_path):
    import httpx
    catalog = {"chat": dict(id="chat", supported_parameters=["tools"],
                            pricing=dict(prompt="0.000001", completion="0.000002")),
               "embed": dict(id="embed", pricing=dict(prompt="0.0000001"))}

    def get(client, url, **kwargs):
        chosen = catalog["embed" if "/embeddings/" in url else "chat"]
        return httpx.Response(200, json={"data": [chosen]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", get)
    provider = LiveProvider("test-key", UsageLedger(), tmp_path / "calls.jsonl")
    assert provider.preflight("chat", "embed")["chat"]["prompt"] == .000001
    with pytest.raises(ValueError, match="absent"):
        provider.preflight("missing-model", "embed")
    assert not provider.trace_path.exists()


def test_sonnet_catalog_prices_allow_unused_web_search_and_cover_one_hour_cache():
    price = dict(prompt="0.000002", completion="0.00001", web_search="0.01",
                 input_cache_read="0.0000001", input_cache_write="0.0000025",
                 input_cache_write_1h="0.000004", discount=0)
    normalized = text_request_pricing(price, "anthropic/claude-sonnet-5.5")
    assert normalized["prompt"] == .000004
    assert normalized["completion"] == .00001
    assert normalized["request"] == 0
    assert normalized["catalog_pricing"] == price
    assert normalized["unused_feature_prices"] == {"web_search": "0.01"}
    ledger = UsageLedger()
    call = ledger.reserve(kind="chat", model="sonnet", input_tokens=1000, output_tokens=100,
                          pricing=normalized, case_id="example")
    assert call["reserved_cost_usd"] == pytest.approx(.005)


def test_unknown_nonzero_fee_names_the_field_and_known_reasoning_fee_is_reserved():
    with pytest.raises(ValueError, match="new_unknown_fee"):
        text_request_pricing(dict(prompt="0.01", new_unknown_fee="0.50"), "model")
    assert text_request_pricing(dict(prompt="0.01", new_unknown_fee="0"), "model")["prompt"] == .01
    price = text_request_pricing(dict(prompt="0.01", completion="0.02", internal_reasoning="0.03"), "model")
    assert price["completion"] == pytest.approx(.05)


def test_optional_prices_do_not_block_catalog_discovery(monkeypatch, tmp_path):
    import httpx

    def get(client, url, **kwargs):
        record = dict(id="embed" if "/embeddings/" in url else "chat",
                      supported_parameters=["tools", "max_tokens"],
                      pricing=dict(prompt="0.000002", completion="0.00001", web_search="0.01",
                                   input_cache_write_1h="0.000004"))
        return httpx.Response(200, json={"data": [record]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", get)
    provider = LiveProvider("test-key", UsageLedger(), tmp_path / "calls.jsonl")
    assert provider.preflight("chat", "embed")["chat"]["prompt"] == .000004
    assert "temperature" not in provider.supported_parameters["chat"]
    assert provider.ledger.summary()["calls"] == 0


def test_chat_request_omits_unsupported_temperature_and_server_plugins():
    arguments = dict(model="chat", messages=[dict(role="user", content="Synthetic request")],
                     system="System instructions", tools=[dict(type="function", function=dict(name="task_email_search"))],
                     max_output_tokens=1200)
    payload = build_chat_payload(**arguments, supported_parameters={"max_tokens", "tools"})
    assert "temperature" not in payload and "plugins" not in payload
    assert payload["tools"][0]["type"] == "function"
    assert payload["max_tokens"] == 1200
    assert build_chat_payload(**arguments, supported_parameters={"temperature"})["temperature"] == 0


def test_catalog_timeout_retries_only_discovery_and_keeps_model_usage_empty(monkeypatch, tmp_path):
    import httpx
    attempts = []

    def get(client, url, **kwargs):
        attempts.append(url)
        raise httpx.ReadTimeout("simulated slow catalog")

    monkeypatch.setattr(httpx.Client, "get", get)
    provider = LiveProvider("test-key", UsageLedger(), tmp_path / "calls.jsonl",
                            catalog_timeout_seconds=60)
    with pytest.raises(CatalogUnavailable, match="chat model catalog") as failure:
        provider.preflight("chat", "embed")
    assert len(attempts) == 2
    assert "60-second timeout" in str(failure.value)
    assert provider.ledger.summary()["calls"] == 0
    assert not provider.trace_path.exists()


def test_transient_catalog_timeout_recovers_without_retrying_model_calls(monkeypatch, tmp_path):
    import httpx
    attempts = []

    def get(client, url, **kwargs):
        attempts.append(url)
        if len(attempts) == 1:
            raise httpx.ReadTimeout("temporary timeout")
        record = dict(id="embed" if "/embeddings/" in url else "chat",
                      supported_parameters=["tools"], pricing=dict(prompt="0", completion="0"))
        return httpx.Response(200, json={"data": [record]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", get)
    provider = LiveProvider("test-key", UsageLedger(), tmp_path / "calls.jsonl")
    assert set(provider.preflight("chat", "embed")) == {"chat", "embed"}
    assert len(attempts) == 3
    assert provider.ledger.summary()["calls"] == 0


def test_snapshot_omits_existing_app_data_and_credentials(tmp_path, monkeypatch):
    from tests.evaluation import evaluate_agent_workflows as evaluator
    source = tmp_path / "source"
    (source / "server/data").mkdir(parents=True)
    (source / "server/data/private.db").write_text("personal data")
    (source / "server/main.py").write_text("code")
    (source / "server/.env").write_text("SECRET=do-not-copy")
    (source / "server/.env.local").write_text("SECRET=also-do-not-copy")
    (source / "tests/evaluation").mkdir(parents=True)
    (source / "tests/fixtures").mkdir()
    (source / "tests/evaluation/runner.py").write_text("code")
    fixture = source / "tests/fixtures/agent_workflow_cases.json"
    fixture.write_text("{}")
    monkeypatch.setattr(evaluator, "ROOT", source)
    monkeypatch.setattr(evaluator, "FIXTURE", fixture)
    target = tmp_path / "copy"
    snapshot(target)
    assert (target / "server/main.py").exists()
    assert not (target / "server/data").exists()
    assert not (target / "server/.env").exists()
    assert not (target / "server/.env.local").exists()
    assert (source / "server/data/private.db").read_text() == "personal data"


def test_model_settings_load_without_copying_mail_credentials(tmp_path, monkeypatch):
    from tests.evaluation import evaluate_agent_workflows as evaluator
    (tmp_path / ".env").write_text(
        "OPENROUTER_API_KEY=fixture-key\nOPENPOKE_MODEL=fixture-model\nCOMPOSIO_API_KEY=private-mail-key\n")
    monkeypatch.setattr(evaluator, "ROOT", tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENPOKE_MODEL", raising=False)
    monkeypatch.setenv("COMPOSIO_API_KEY", "existing-mail-key")
    env = evaluator.evaluation_environment()
    assert env["OPENROUTER_API_KEY"] == "fixture-key"
    assert env["OPENPOKE_MODEL"] == "fixture-model"
    assert "COMPOSIO_API_KEY" not in env
    assert evaluator.evaluation_environment("explicit-model")["OPENPOKE_MODEL"] == "explicit-model"


def test_fixture_covers_routing_ambiguity_approval_and_failure():
    fixture = load_fixture()
    assert len(fixture["cases"]) == 9
    assert sum(len(c["turns"]) for c in fixture["cases"]) == 10
    assert {"identity_collision", "paraphrase", "clarification", "new_work",
            "continuity", "approval", "tool_failure", "archived_reuse"} <= {
                c["category"] for c in fixture["cases"]}
