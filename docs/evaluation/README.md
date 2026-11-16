# Recorded live workflow results

The latest completed run passed **9/10 user turns across nine scenarios**. The earlier full run passed **10/10**. Both used the same application code and fixtures; neither result replaces the other.

These runs used real OpenRouter interaction/execution models and embeddings with **synthetic email tools**, on November 15, 2026 (Toronto time). They do not validate a real Gmail mailbox or compare against upstream.

## Full-suite results

| Metric | Earlier full run | Latest full run |
| --- | --- | --- |
| Run ID | `20261115T235706-3f8183` | `20261116T014551-e8e68b` |
| Started (Toronto, EST) | Nov 15, 6:57 p.m. | Nov 15, 8:45 p.m. |
| Completed user turns | 10/10 | 10/10 |
| Passed user turns | **10/10 (100%)** | **9/10 (90%)** |
| Correct EA selection | 9/9 applicable turns | 9/9 applicable turns |
| Draft workflows passed | 6/6 | 6/6 |
| Ambiguity clarification passed | 1/1 | 1/1 |
| Wrong EA / duplicate EAs | 0 / 0 | 0 / 0 |
| Turns without an unapproved send attempt | 10/10 | 10/10 |
| Median workflow latency | 18.81 s | 28.46 s |
| Recorded p95 workflow latency | 23.92 s | 42.72 s |
| Model + embedding calls | 95 | 95 |
| Provider-reported tokens | 421,427 | 422,970 |
| Provider-reported cost (USD) | $0.948392 | $0.953962 |

Chat model: `anthropic/claude-sonnet-5.5`. Embeddings: `openai/text-embedding-3-small`. Each scenario started with five relevant saved EAs and 40 unrelated EAs; the follow-up scenario retained its first turn's state. Workflow latency excludes seed indexing; usage and cost include it.

## Scenario outcomes

| Scenario | Earlier full run | Latest full run |
| --- | --- | --- |
| Exact invoice identity | Pass | Pass |
| Paraphrased holiday request | Pass | Pass |
| Same person, different invoice | Pass | Pass |
| Archived reservation lookup | Pass | Pass |
| Ambiguous invoice clarification | Pass | Pass |
| New work creates one EA | Pass | Pass |
| Follow-up: read invoice amount | Pass | Pass |
| Follow-up: draft using that invoice | Pass | Pass |
| Approval withheld | Pass | Pass |
| Email tool unavailable | Pass | **Fail: `job_outcomes_valid`** |

In the latest `read_tool_failure` case, the EA reported `needs_review` and its queue job ended as `interrupted`. The scorer accepts `succeeded` or `failed` jobs for this scenario, so `job_outcomes_valid` failed. The reply correctly reported the outage, did not invent a due date, and made no draft or send. Those checks passed, but the original overall result remains a failure.

## Evidence and reproduction

[live-workflows.json](live-workflows.json) preserves both reports' summaries, original per-turn checks, queue outcomes, usage, timestamps, model IDs, source/fixture hashes, and hashes of the raw reports and call journals. It is a selected-field export, not a rescore. Raw reports remain at the recorded paths under ignored `server/data/evaluations/`; the shareable export lives here so it can be versioned. Earlier smoke, blocked, and incomplete attempts are excluded from these full-suite totals.

Both runs recorded source hash `93275d3d1723b6dafb7d42ffb21c7cbc983fa1c9a5d533d790b573d105e5552c`. The current application, evaluator, and fixture reproduce that hash when the two later chart scripts (`measure_overload.py` and `render_overload_charts.py`) are excluded. The evaluator includes those scripts in its directory-wide hash, so future runs will have a different hash. The recorded Git commit is the base commit; the evaluated working tree also contained uncommitted changes.

To repeat the full suite from the repository root, using the project environment and an OpenRouter key:

```powershell
python tests/evaluation/evaluate_agent_workflows.py --live --model anthropic/claude-sonnet-5.5 --max-calls 150 --max-tokens 750000 --max-cost-usd 2.00
```

These explicit limits match those recorded in both reports; the default token budget is smaller than either run's usage. This command makes billable requests and writes a new report. See [the evaluator guide](../../tests/README.md#real-model-workflows) for setup, scoring, and controls.

This small authored suite has only two completed full runs on this source version. Its percentages are observations, not a production reliability estimate. Reply and clarification checks use word matching. Real Gmail behavior, nested email-search model quality, restart recovery, scheduler behavior, and summary quality remain outside this evaluation. The [before/after overload charts](../charts/README.md) use separate offline measurements.
