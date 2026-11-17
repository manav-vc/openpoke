# Tests and evaluation

All automated tests, evaluation code, and labeled fixtures live in this directory.

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
| [test_workflow_comparison.py](test_workflow_comparison.py) | Checks the shared upstream/fork outcome contract and usage accounting. |
| [test_memory_evaluation.py](test_memory_evaluation.py) | Rejects wrong corrections, unsupported citations, and unauthorized memory-probe writes. |
| [test_hard_retrieval_evaluation.py](test_hard_retrieval_evaluation.py) | Checks dense task collisions, retrieval metrics, and numeric fact scoring. |
| [conftest.py](conftest.py) | Configures offline test defaults and blocks real HTTP requests. |
| [evaluation/evaluate_agent_retrieval.py](evaluation/evaluate_agent_retrieval.py) | Scores 60 labeled retrieval cases and benchmarks search with 1,000 and 10,000 saved agents. |
| [fixtures/agent_retrieval_cases.json](fixtures/agent_retrieval_cases.json) | Defines synthetic agents and labeled exact-reference, paraphrase, ambiguity, archive, no-match, and identity-conflict cases. |
| [evaluation/evaluate_agent_workflows.py](evaluation/evaluate_agent_workflows.py) | Runs real interaction/execution models and embeddings with controlled email tools across nine scenarios and ten user turns. |
| [fixtures/agent_workflow_cases.json](fixtures/agent_workflow_cases.json) | Defines workflow requests and expected owners, actions, facts, clarification, and approval behavior. |
| [evaluation/compare_agent_workflows.py](evaluation/compare_agent_workflows.py) | Runs the same authored requests, model, saved tasks, and synthetic mailbox through upstream and the fork. |
| [evaluation/evaluate_agent_memory.py](evaluation/evaluate_agent_memory.py) | Generates real summaries of 72 historical exchanges, then verifies exact answers against retrieved source records. |
| [evaluation/evaluate_agent_retrieval_hard.py](evaluation/evaluate_agent_retrieval_hard.py) | Compares three retrieval modes on 288 plausible competing agents and runs four full IA/EA turns. |

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

[Recorded results and per-turn evidence](../docs/evaluation/README.md) include the final **10/10 fork versus 6/10 upstream** shared-contract comparison and preserve the original **10/10 and 9/10** runs on identical earlier source. The historical unavailable-email scenario failed because a model-selected `needs_review` state incorrectly interrupted the job. That runtime bug is fixed and passes in the new full run. The results page also includes the dense retrieval ablation and **4/4** production-default memory evaluation.

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

Passing a draft case requires the correct agent, recipient, thread, requested facts, exactly one persisted draft shown verbatim to the user, and a pending approval. A blocked send attempt still fails the case. Read-only answers may use the correct saved source without creating a redundant execution job. Clarifications must ask a relevant distinguishing question and perform no work. Incomplete runs always show coverage and cannot pass overall. Exit codes: **0** complete pass, **1** complete with failures, **2** blocked/incomplete.

This base suite is small and authored; it is not a production accuracy estimate. The paired runner below adds an upstream comparison, and the separate memory runner evaluates compaction. Reply/clarification checks use disclosed word matching and may reject valid paraphrases. Seed indexing is measured separately from workflow latency. Compaction and automatic archiving are disabled in workflow cases; existing archived-agent reuse is still exercised. Nested Gmail search-model quality, real mailbox behavior and restart recovery remain outside these live workflows. Model behavior and timings vary across runs.

## Controlled upstream comparison

The paired runner reads upstream `5b5f635935a64ab37884c025d70abb0ed731c094` from Git and runs both source versions in separate temporary directories. It pins the same chat model, including upstream's older hardcoded settings. Both receive the same requests, mailbox messages, saved task names and facts, and 40 distractors. Native prompts, tools, context construction and agent loops remain implementation-specific.

```powershell
python tests/evaluation/compare_agent_workflows.py
python tests/evaluation/compare_agent_workflows.py --live --model anthropic/claude-sonnet-5.5
```

Each arm has a separate $2 estimated budget, 150-call limit and 750,000-token limit. Use `--case ID` for a subset or `--only upstream` / `--only fork` to run one arm. Reports retain original checks. Shared task success measures correct persisted actions and facts, correct task reuse, clarification, and no attempted send; exact preview and persisted approval metadata are reported separately because upstream lacks the fork's approval-state mechanism. A draft case requires an EA-owned persisted email-tool draft, so an IA-only text preview does not pass that contract. Chat prompt tokens include every per-turn IA/EA call. Workflow latency excludes embedding indexing; setup usage is recorded separately. HTTP 402 stops further provider calls rather than repeatedly testing an unavailable account.

The upstream import adapter bypasses eager web-server initialization. On Windows it supplies the Unix roster-lock API using a real Windows file lock. External email tools are replaced at the boundary, and batch delivery tasks are observed without replacing native agent loops. This does not evaluate startup, real Gmail, or multi-process locking. See [saved results and limitations](../docs/evaluation/README.md).

## Long-history accuracy

```powershell
python tests/evaluation/evaluate_agent_memory.py
python tests/evaluation/evaluate_agent_memory.py --live
```

The fixture has 72 complete historical exchanges with an old amount, a later correction, exact invoice/purchase-order/bank references, a created draft and explicit withheld approval. Production `HistoryService.compact` generates summaries with the configured model; the final summary must cover all earlier complete exchanges. Every source event must remain intact. Production EA runs then answer four read-only probes with exact structured facts and citations. Passing requires the necessary original events to have been retrieved, all citations to refer to retrieved events, no attempted write, and pending approval to remain intact. Labels are used only by the scorer.

Defaults use the submitted 8,000-token history / 2,000-token summary budgets. `--history-tokens` and `--summary-tokens` allow stress configurations. `--with-baseline` additionally runs the same probes with bounded recent history and source retrieval but no summary; this is an ablation of the working view, not upstream. This tests the combined summary-and-source-retrieval design; it does not claim summaries alone preserve every fact. Earlier incomplete attempts remain separate from the final report.

## Dense retrieval and ablation

```powershell
python tests/evaluation/evaluate_agent_retrieval_hard.py
python tests/evaluation/evaluate_agent_retrieval_hard.py --live
```

The corpus has 12 contacts with 24 invoices each: 288 plausible saved tasks, shared email threads, overlapping purposes, changed instructions and archived work. Thirty-six queries include 34 labeled targets and two intentionally ambiguous requests. Hybrid, keyword-only and semantic-only modes share production identity guards; only ranking channels differ. Real embeddings are reused between ablations, so differences do not come from separate provider calls. Report recall@10 and @30 by category; exact-ID resolution bypasses ranking and is disclosed separately.

Four additional user turns run the complete production IA/EA, queue and delivery paths over that corpus. They check exact invoice selection, a paraphrased near collision, corrected instructions, and clarification. Numeric reply scoring accepts thousands separators, verifies the corrected amount and rejects the superseded amount. The original word-match result and the corrected score are both saved; the model answer is not regenerated. Default billable spend is capped at $1 estimated. See [saved evidence](../docs/evaluation/README.md).

## Before/after charts

[The three overload charts](../docs/charts/README.md) compare directory exposure, synthetic history context, and controlled execution admission against upstream `5b5f635`. The committed measurements include source hashes and a saved retrieval benchmark excerpt.

```powershell
python tests/evaluation/measure_overload.py
python -m pip install matplotlib
python tests/evaluation/render_overload_charts.py
```

The [updated evaluation images](../docs/charts/README.md) also show workflow success, retrieval quality, memory preservation and the token/latency tradeoff from the saved reports. Their evidence and limitations are documented alongside the images.

No model or Gmail calls are made. Concurrency uses simulated one-second jobs and actual SQLite admission; the asynchronous scheduler and real execution latency are outside this fixture. Token values are estimates, and the tokenizer/fallback is recorded with the measurements.
