# Tests and evaluation

All automated tests, evaluation code, and labeled fixtures live in this directory.

| File | Description |
| --- | --- |
| [test_budgets.py](test_budgets.py) | Checks token limits, safe text truncation, and valid JSON excerpts. |
| [test_execution_queue.py](test_execution_queue.py) | Checks queue limits, ordering, timeouts, retries, and recovery without live APIs. |
| [test_execution_runtime.py](test_execution_runtime.py) | Checks execution approvals, tool effects, source evidence, and uncertain writes. |
| [test_interaction_routing.py](test_interaction_routing.py) | Checks task routing, candidate budgets, identity guards, and crash replay offline. |
| [test_registry_history.py](test_registry_history.py) | Checks stable task identity, migration, archival, source history, and compaction. |
| [test_retrieval.py](test_retrieval.py) | Checks hybrid retrieval, identity matching, embedding failures, and candidate limits. |
| [test_runtime_integration.py](test_runtime_integration.py) | Checks registry, queue, interaction, trigger, and reset integration with fake models. |
| [conftest.py](conftest.py) | Configures offline test defaults and blocks real HTTP requests. |
| [evaluation/evaluate_agent_retrieval.py](evaluation/evaluate_agent_retrieval.py) | Scores 60 labeled retrieval cases and benchmarks search with 1,000 and 10,000 saved agents. |
| [fixtures/agent_retrieval_cases.json](fixtures/agent_retrieval_cases.json) | Defines synthetic agents and labeled exact-reference, paraphrase, ambiguity, archive, no-match, and identity-conflict cases. |

Run these commands from the repository root:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
python tests/evaluation/evaluate_agent_retrieval.py --benchmark --output server/data/agent_evaluation_report.json
```

Pytest discovers the `test_*.py` files and imports the evaluator for an offline retrieval check. The evaluator's benchmarks run only when requested with `--benchmark`.

The default evaluator uses deterministic stand-in embeddings and temporary synthetic data. Reports are generated under `server/data/` with the commands above. Passing requires recall@30 of at least 95%, zero identity-conflict violations, zero ambiguity-guard failures, and an empty indexing backlog. These checks do not measure live interaction-model selection or Gmail workflow success.

To evaluate hosted embeddings, explicitly opt into billable OpenRouter requests:

```powershell
python tests/evaluation/evaluate_agent_retrieval.py --live --output server/data/agent_live_evaluation_report.json
```

Live mode requires `OPENROUTER_API_KEY`, uses synthetic tasks, and does not access Gmail.
