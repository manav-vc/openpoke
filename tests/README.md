# Tests and evaluation

All automated tests, evaluation code, and labeled fixtures live in this directory.

[Results and status](../docs/evaluation/README.md#validation-status) · [Run checks](#run-checks) · [Real-model workflows](#real-model-workflows) · [Charts](#beforeafter-charts)

Recorded results are historical. The current **100-concurrent** default needs a fresh runtime/admission check. See the [current revision status](../docs/evaluation/README.md#current-revision) and [execution/EA state guide](../docs/evaluation/README.md#execution-status-and-ea-state).

| File | Description |
| --- | --- |
| [test_budgets.py](test_budgets.py) | Checks token limits, safe text truncation, and valid JSON excerpts. |
| [test_execution_queue.py](test_execution_queue.py) | Checks queue limits, ordering, timeouts, retries, and recovery without live APIs. |
| [test_execution_runtime.py](test_execution_runtime.py) | Checks execution approvals, tool effects, source evidence, and uncertain writes. |
| [test_draft_presentation.py](test_draft_presentation.py) | Checks that IA previews use exact pending EA draft sources, including account isolation, large artifacts, and sent/deleted drafts. |
| [test_interaction_routing.py](test_interaction_routing.py) | Checks task routing, candidate budgets, identity guards, and crash replay offline. |
| [test_registry_history.py](test_registry_history.py) | Checks stable task identity, migration, archival, source history, and compaction. |
| [test_retrieval.py](test_retrieval.py) | Checks hybrid retrieval, identity matching, embedding failures, and candidate limits. |
| [test_runtime_integration.py](test_runtime_integration.py) | Checks registry, queue, interaction, trigger, and reset integration with fake models. |
| [test_workflow_evaluation.py](test_workflow_evaluation.py) | Checks workflow scoring, spend reservations, partial reports, and data isolation without live calls. |
| [conftest.py](conftest.py) | Configures offline test defaults and blocks real HTTP requests. |
| [evaluation/evaluate_agent_retrieval.py](evaluation/evaluate_agent_retrieval.py) | Scores 60 labeled retrieval cases and benchmarks search with 1,000 and 10,000 saved agents. |
| [fixtures/agent_retrieval_cases.json](fixtures/agent_retrieval_cases.json) | Defines synthetic agents and labeled exact-reference, paraphrase, ambiguity, archive, no-match, and identity-conflict cases. |
| [evaluation/evaluate_agent_workflows.py](evaluation/evaluate_agent_workflows.py) | Runs real interaction/execution models and embeddings with controlled email tools across nine scenarios and ten user turns. |
| [fixtures/agent_workflow_cases.json](fixtures/agent_workflow_cases.json) | Defines workflow requests and expected owners, actions, facts, clarification, and approval behavior. |

## Run checks

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

## Real-model workflows

This optional evaluation runs the application's interaction and execution loops, hybrid search, persistent queue, and result delivery with **real OpenRouter models and embeddings**. Email search, draft creation, and sending are replaced with a synthetic mailbox. It never connects to Gmail.

The nine scenarios cover exact identifiers, paraphrases, two invoices for the same person, archived work, ambiguity, new work, a two-turn follow-up, withheld approval, and an unavailable email tool. Each scenario starts with five saved tasks and 40 unrelated agents. Expected answers are used only by the scorer, never supplied to the runtime.

[Recorded results and per-turn evidence](../docs/evaluation/README.md): the latest completed full run passed **9/10 turns**; the earlier full run passed **10/10**, using the same application code and fixtures. Both passed all six draft workflows and selected the correct EA in all nine applicable turns. The latest unavailable-email scenario failed `job_outcomes_valid` because its job ended as `interrupted`, despite correctly reporting the outage without guessing a due date. Both runs used real models with synthetic email tools.

Validate fixtures without making model calls:

~~~powershell
python tests/evaluation/evaluate_agent_workflows.py
~~~

Start with one billable smoke case, using your configured OPENPOKE_MODEL and OPENROUTER_API_KEY:

~~~powershell
python tests/evaluation/evaluate_agent_workflows.py --live --case exact_invoice --max-calls 20 --max-cost-usd 0.50
~~~

Run the full suite, optionally overriding the chat model or repeating it:

~~~powershell
python tests/evaluation/evaluate_agent_workflows.py --live
python tests/evaluation/evaluate_agent_workflows.py --live --model YOUR_OPENROUTER_MODEL_ID --repeat 3
~~~

The recorded full runs used larger call/token limits than the defaults. To repeat their configuration:

~~~powershell
python tests/evaluation/evaluate_agent_workflows.py --live --model anthropic/claude-sonnet-5.5 --max-calls 150 --max-tokens 750000 --max-cost-usd 2.00
~~~

Defaults cap the entire run at **100 model/embedding calls, 250,000 accounted tokens, $2.00 in estimated cost reservations, 150 seconds per turn, and 900 seconds overall**. Repeats share these limits; raise them explicitly if needed. Pricing and tool support are checked against the provider catalog first. Catalog reads allow 30 seconds and one retry; use --catalog-timeout 60 for a slow connection. Reservations use estimated tokens; provider-reported charges are recorded separately. Failed calls with unknown usage retain their reservation. Billable model/embedding requests are never retried by the transport, and there are no offline model fallbacks.

Cost reservations use the largest advertised input/cache rate, include any separately listed reasoning rate, and assume no discount. Optional image/audio/video/web-search prices are recorded but do not apply to these text-only requests without server tools. Unknown nonzero fees are named in the error and remain blocked. Raw catalog pricing is preserved in the report, including when validation fails. Temperature is set only when the chosen model advertises support.

The runner prints a unique output location under server/data/evaluations/. The report.json file contains per-turn checks, queue jobs, persisted synthetic actions, source events, failures, source/fixture hashes, model IDs, usage, and workflow latency. The adjacent report.calls.jsonl journals reservations before requests and records model responses and usage afterward. Credentials and private model reasoning fields are excluded. Use a fresh --output PATH to choose another report location.

Report updates retry temporary Windows file locks. If replacement stays blocked, the error names a complete preserved snapshot; the previous report remains intact. For persistent sync/editor locks, close the file or choose a fresh --output path outside the synced folder.

### Result states

| Exit code | State | Interpretation |
| --- | --- | --- |
| `0` | PASS | Every planned turn completed and passed. |
| `1` | FAIL | All turns completed, with one or more failures. |
| `2` | BLOCKED / INCOMPLETE | Setup failed or coverage is incomplete; cannot count as a pass. |

A report's `status: completed` alone does not mean all checks passed. Inspect `summary.complete`, `summary.all_passed`, coverage, and each turn's checks. [Recorded scenarios](../docs/evaluation/README.md#scenario-outcomes) show both execution outcomes and EA work states.

Passing a draft case requires the correct agent, recipient, thread, requested facts, exactly one persisted draft shown verbatim to the user, and a pending approval. A blocked send attempt still fails the case. Read-only answers may use the correct saved source without creating a redundant execution job. Clarifications must ask a relevant distinguishing question and perform no work. Incomplete runs always show coverage and cannot pass overall. Exit codes: **0** complete pass, **1** complete with failures, **2** blocked/incomplete.

This is a small authored exploratory suite, not production accuracy or an upstream baseline comparison. Reply/clarification checks use disclosed word matching and may reject valid paraphrases. Seed indexing is measured separately from workflow latency. Compaction and automatic archiving are disabled for reproducibility; existing archived-agent reuse is still exercised. Nested Gmail search-model quality, real mailbox behavior, restart recovery, and summary quality remain outside this evaluation. Model behavior and timings vary across runs, as the two recorded full runs demonstrate.

## Before/after charts

[The three overload charts](../docs/charts/README.md) compare directory exposure, synthetic history context, and controlled execution admission against upstream `5b5f635`. The committed measurements include source hashes and a saved retrieval benchmark excerpt. The concurrency fixture deliberately sets **4 workers / 24 jobs**; it is historical evidence and does not validate the current **100-worker** default. A new admission run must record its own limit, burst size, and source revision.

```powershell
python tests/evaluation/measure_overload.py
python -m pip install matplotlib
python tests/evaluation/render_overload_charts.py
```

No model or Gmail calls are made. Concurrency uses simulated one-second jobs and actual SQLite admission; the asynchronous scheduler and real execution latency are outside this fixture. Token values are estimates, and the tokenizer/fallback is recorded with the measurements.
