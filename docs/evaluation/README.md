# Validation results and evidence

[Status guide](#validation-status) · [Latest run](#latest-full-run) · [Earlier run](#earlier-full-run) · [Scenarios and states](#scenario-outcomes) · [Offline evidence](#offline-evidence) · [Rerun](#reproduction)

## Validation status

| Label | Meaning |
| --- | --- |
| **PASS** | The recorded run completed and every scored turn passed. |
| **FAIL** | The recorded run completed, but at least one scored turn failed. |
| **BLOCKED / INCOMPLETE** | Setup failed or fewer than all planned turns were evaluated. It cannot count as a pass. |
| **PENDING** | A check has not been rerun on the current revision. |
| **NOT TESTED** | The behavior is outside the recorded evaluation's scope. |

Run status `completed` describes coverage, not success. Both full runs completed **10/10 turns**; the latest still **failed overall**. Earlier smoke, blocked, and incomplete attempts are excluded from their totals.

## Current revision

**PENDING:** the repository now defaults to **100 concurrent executions**, **100 waiting**, and **one active execution per EA**. The recorded real-model results and four-worker admission measurement below predate that change. Fresh runtime-suite and 100-concurrent admission results are pending for this revision.

The original reports, hashes, checks, and observed failures remain unchanged.

## Latest full run

**FAIL — 9/10 turns (90%); coverage 10/10.** Run `20261116T014551-e8e68b`, started **November 15, 2026, 8:45 p.m. Toronto time**.

| Check | Result |
| --- | --- |
| Correct EA selection | **9/9** applicable turns |
| Draft workflows | **6/6** |
| Relevant ambiguity clarification | **1/1** |
| Wrong EA / duplicate EAs | **0 / 0** |
| Turns without an unapproved send attempt | **10/10** |
| Median / recorded p95 workflow latency | **28.46 s / 42.72 s** |
| Model + embedding calls | **95** |
| Provider-reported tokens / cost (USD) | **422,970 / $0.953962** |

**Failed check:** `read_tool_failure → job_outcomes_valid`. The execution ended as `interrupted` and its EA as `needs_review`; the scorer accepts `succeeded` or `failed` for that scenario. Reporting the outage, avoiding an invented due date, and making no draft or send all passed. The overall failure is preserved.

## Earlier full run

**PASS — 10/10 turns (100%); coverage 10/10.** Run `20261115T235706-3f8183`, started **November 15, 2026, 6:57 p.m. Toronto time**.

EA selection **9/9**, draft workflows **6/6**, clarification **1/1**; **0** wrong EAs, duplicate EAs, or unapproved send attempts. Median / recorded p95 latency: **18.81 s / 23.92 s**. **95** calls, **421,427** provider-reported tokens, **$0.948392 USD**.

Both runs used the same evaluated application and fixtures, with `anthropic/claude-sonnet-5.5` and `openai/text-embedding-3-small`. These are recorded model IDs. Email tools were synthetic. Each scenario started with five relevant and 40 unrelated EAs; the two follow-up turns shared state. Workflow latency excludes seed indexing; usage includes it.

## Scenario outcomes

| Scenario / user turn | Earlier | Latest | Latest execution status | Latest EA work state |
| --- | --- | --- | --- | --- |
| Exact invoice identity | PASS | PASS | `succeeded` | `waiting_for_user` |
| Paraphrased holiday request | PASS | PASS | `succeeded` | `waiting_for_user` |
| Same person, different invoice | PASS | PASS | `succeeded` | `waiting_for_user` |
| Archived reservation lookup | PASS | PASS | No execution needed | — |
| Ambiguous invoice clarification | PASS | PASS | No execution needed | — |
| New work creates one EA | PASS | PASS | `succeeded` | `waiting_for_user` |
| Follow-up: read invoice amount | PASS | PASS | No execution needed | — |
| Follow-up: draft using that invoice | PASS | PASS | `succeeded` | `waiting_for_user` |
| Approval withheld | PASS | PASS | `succeeded` | `waiting_for_user` |
| Email tool unavailable | PASS | **FAIL** | `interrupted` | `needs_review` |

### Execution status and EA state

| Record | States | Meaning |
| --- | --- | --- |
| Execution | `queued`, `running` | Work has not reached an outcome. |
| Execution | `succeeded`, `failed`, `expired`, `interrupted`, `rejected` | Terminal outcomes. Each counts toward settling a sealed result group. |
| EA | `open`, `waiting_for_user`, `waiting_for_trigger`, `completed`, `needs_review` | The ongoing work's state, independent of one run's result. |

A successful draft execution can leave its EA `waiting_for_user` for approval. A terminal `interrupted` execution can leave its EA `needs_review` and hold queued follow-ups. Terminal does not mean successful.

## Offline evidence

**PASS — historical record, November 14, 2026.** The recorded suite passed **129 automated tests**; **60/60** labeled retrieval cases found the expected EA in the top 30, with zero identity-conflict or ambiguity-guard failures. Candidate pools stayed at **30** with 1,000 and 10,000 saved EAs. These are historical results, not a test count or pass claim for this revision.

[Committed measurements](../charts/measurements.json) preserve the retrieval benchmark excerpt and source hashes. The full offline report is generated under ignored `server/data/`; it is not included in this checkout.

| Recorded illustration | Before | After | Scope |
| --- | --- | --- | --- |
| IA roster at 10,000 EAs | 10,000 entries | 30 candidates | Candidate exposure; no total-prompt-token claim |
| History at 100 exchanges | 59,459 estimated tokens | 4,529 estimated tokens | Synthetic fixture; summary quality not evaluated |
| 24 independent jobs | 24 immediate executions | 4 active, 20 waiting initially | Historical **four-worker** fixture; all 24 completed |

[View the three charts and methods](../charts/README.md). The four-worker result is preserved as measured; it is not relabeled as a 100-worker benchmark.

## Evidence and source version

[live-workflows.json](live-workflows.json) contains both full-run summaries, per-turn checks and outcomes, usage, timestamps, model IDs, and source/fixture hashes. It is a selected-field export, not a rescore. Raw report and call-journal paths and hashes are retained; those files are under ignored `server/data/evaluations/`.

Both runs recorded evaluated source hash `93275d3d1723b6dafb7d42ffb21c7cbc983fa1c9a5d533d790b573d105e5552c`. At export time, excluding the two later chart scripts reproduced it. The JSON's `provenance.current_source_sha256` describes that export-time snapshot, not this branch after the concurrency update. The recorded Git commit was a base commit; the evaluated working tree included uncommitted changes.

## Reproduction

Use [the test guide](../../tests/README.md#run-checks) for offline commands and [the workflow guide](../../tests/README.md#real-model-workflows) for setup and scoring. To repeat the recorded model configuration:

```powershell
python tests/evaluation/evaluate_agent_workflows.py --live --model anthropic/claude-sonnet-5.5 --max-calls 150 --max-tokens 750000 --max-cost-usd 2.00
```

This makes billable requests with synthetic email tools and writes a new report. Its explicit limits match the recorded runs; the default token budget is smaller than their usage. Workflow runs do not load-test the global 100-execution limit.

## Coverage limits

**NOT TESTED by the real-model runs:** a real Gmail mailbox, nested email-search model quality, automatic scheduling, restart recovery, compaction/summary quality, and global concurrency under load. Offline tests cover some mechanics separately.

This is a small authored exploratory suite with two completed full runs, not an upstream comparison or a production reliability estimate. Reply and clarification checks use disclosed word matching. Compaction and auto-archiving were disabled for reproducibility; archived-EA reuse was exercised.
