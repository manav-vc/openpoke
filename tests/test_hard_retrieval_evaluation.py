"""Meaningful checks for dense-corpus labels and ranked, rather than set, recall."""
from tests.evaluation.evaluate_agent_retrieval_hard import build_fixture, score_ranking, ranking_summary, score_hard_turn


def test_corpus_contains_real_collisions_and_all_labels_resolve():
    fixture = build_fixture()
    assert len(fixture["agents"]) == 288
    by_key = {a["key"]: a for a in fixture["agents"]}
    assert len(by_key) == 288
    assert len({a["invoice_id"] for a in by_key.values()}) == 288
    for contact in range(12):
        same_person = [a for a in by_key.values() if a["key"].startswith(f"contact-{contact}-")]
        assert len(same_person) == 24
        assert len({a["email"] for a in same_person}) == 1
        assert len({a["thread_id"] for a in same_person}) == 1
        assert len({a["purpose"] for a in same_person}) == 24
    assert sum(a["changed"] for a in by_key.values()) == 2
    assert sum(a["archived"] for a in by_key.values()) == 4
    for case in fixture["queries"]:
        assert set(case["expected_keys"]) <= by_key.keys()
    for case in fixture["cases"]:
        for turn in case["turns"]:
            if turn.get("owner"):
                assert turn["owner"] in by_key


def test_recall_keeps_ranking_and_excludes_ambiguity_from_denominator():
    case = dict(id="ranking", category="paraphrase", expected_keys=["target"])
    row = score_ranking(case, [f"other-{i}" for i in range(10)] + ["target"], False)
    ambiguous = score_ranking(dict(id="amb", category="ambiguity", expected_keys=[],
                                  requires_clarification=True), ["target"], False)
    summary = ranking_summary([row, ambiguous])
    assert summary["expected_targets"] == 1
    assert summary["recall_at_10"] == 0
    assert summary["recall_at_30"] == 1
    assert summary["ambiguity_failures"] == 1
    assert summary["by_category"]["ambiguity"]["recall_at_10"] is None


def test_numeric_facts_accept_thousands_separator_without_accepting_wrong_amount():
    expected = dict(reply_amounts=[1180], forbidden_reply_amounts=[515])
    checks = dict(correct_owner=True, required_reply_facts=True)
    assert score_hard_turn(expected, dict(reply="CAD 1,180.00, collection on hold", checks=checks))["passed"]
    assert score_hard_turn(expected, dict(reply="CAD 1,180. Collection on hold.", checks=checks))["passed"]
    assert score_hard_turn(expected, dict(reply="CAD 1180, collection on hold", checks=checks))["passed"]
    assert not score_hard_turn(expected, dict(reply="CAD 11,800, collection on hold", checks=checks))["passed"]
    assert not score_hard_turn(expected, dict(reply="CAD 1,180, formerly 515", checks=checks))["passed"]
