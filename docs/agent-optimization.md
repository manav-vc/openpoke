# Agent overload optimization

OpenPoke stores task identity separately from an executing worker. It can retain 10,000 saved tasks while showing the interaction model at most 30 compact candidates per turn, ten at a time. The chat UI and its 202/poll-history flow are unchanged.

For the take-home assessment, see the [solution board and sequence diagram](diagrams/README.md).

## Routing and identity

The interaction agent resolves explicit stable IDs and persisted conversation links first. Otherwise it searches the registry, compares compact candidate descriptions, and inspects a current record before dispatch. Keyword and semantic rankings each contribute 30 results; reciprocal rank fusion uses k=60. Confirmed conflicting invoice/case/task identifiers exclude a candidate. Contacts and email threads can legitimately belong to several tasks.

Tools are `search_agents`, `inspect_agent`, `create_agent`, `send_message_to_agent`, `read_agent_history`, `archive_agent`, `restore_agent`, and `resolve_interrupted_execution`, alongside the existing reply/draft/wait tools. Creation is explicit and requires archive-inclusive discovery. Dispatch requires the version returned by inspection. Searching or inspecting does not count as task activity.

Search hits produce observed references. Execution captures actual Gmail IDs and recipients at the tool boundary. `confirm_task_reference` promotes a task-specific identifier only when it appears in a cited tool result for that task. Model-generated metadata alone is not trusted provenance.

OpenRouter embeds short registry descriptions using `openai/text-embedding-3-small`. Normalized float32 vectors are stored with model, dimensions, and content version. A cached NumPy matrix performs exact cosine search outside the event loop. Missing/stale vectors and provider failures are visible degraded-search states; keyword search continues and remains bounded. Indexing retries through a durable outbox.

## Storage and migration

The default database is `server/data/agents/registry.db`. It contains registry records, scoped references, FTS5, vectors/index work, full source events/artifacts, summaries, execution jobs/batches, and interaction response/tool-plan journals. SQLite needs FTS5 support. No external vector service is required.

Startup imports the legacy `execution_agents/roster.json`, task logs, and trigger owners idempotently. Original histories and roster files remain unchanged until an explicit history reset. Display names may change without changing task identity. Legacy aliases retain trigger ownership. New UUID owners are not reimported as historical names. Colliding legacy log filenames are marked ambiguous rather than guessed apart.

Legacy tool logs were already truncated to 200/500 characters; missing original information cannot be restored by summarization. New executions preserve complete structured arguments/results. Large results use scoped artifact references. Source reads support event IDs, query-centered excerpts, older-event cursors, and `source:<event_id>:<offset>` cursors for the remainder of a large source.

Execution context combines structured task state, an earlier-work summary, and recent complete request segments. Context token counts are conservative estimates with a margin, not exact provider guarantees. Provider-reported usage is logged for tuning. Source records remain available after compaction.

## Queue, result groups, and recovery

Run **one backend process**. Multiple server workers and distributed leases are outside this implementation. Use the normal development reload server, or a single production worker.

All chat and trigger executions use one queue: four concurrent executions, at most 100 queued jobs, five minutes maximum queue wait, and 300 seconds execution time after work starts. These are configurable. The execution deadline bounds stalled tools, model loops, and worker use; individual model calls also have HTTP timeouts. Oldest runnable work starts first, and a task has at most one running execution. A busy or review-blocked task cannot occupy slots needed by other agents.

Batch membership is saved with each submission and sealed when its initiating interaction finishes. A group is delivered only after sealing and all members become terminal, including rejected/expired/failed/interrupted jobs. Follow-up stages use separate correlated groups. Defaults limit one initiating request to five follow-up stages and 20 submissions.

Queued work resumes after restart if it has not expired. Timeouts and interruptions are classified using the actual dispatched tools in the durable journal, not instructions claiming a task is read-only. Known reads become failed, retryable executions and leave the task available for new work. Saved results remain retrievable. A write or unknown tool requires review, including a timeout after an earlier successful write. New submissions for a review-blocked task are rejected immediately with its blocking execution ID. Already queued follow-ups remain held. OpenPoke does not automatically repeat actions that may already have happened. Inspection exposes interrupted execution IDs. After checking the original Gmail/task state, tell OpenPoke to mark the interruption completed, abandon it, or explicitly retry it. Retrying records a new attempt and retains the old outcome; a failed reviewed retry preserves review of the original uncertain action.

A request permits at most one automatic retry of safe read-only interrupted work. The queue persists that retry's source execution, so another delivery or replacement agent cannot reset the allowance. Expired, blocked, rejected, and uncertain work is not retried automatically. Fresh user requests have a new budget. Legacy read-only timeouts can be reconciled using `ExecutionService.reconcile_read_only_interruption`, which verifies the journal, preserves the original error in an audit event, and refuses unknown or mutating work.

The interaction model's responses and tool-call plans are persisted before effects. Queue submissions and conversation replies/drafts use stable operation/event keys. Recovery resumes committed steps rather than blindly repeating an action-capable result notification. An ordinary terminal interaction error produces one visible saved-work error; execution results and histories remain available for a user-directed continuation.

Reminder occurrences are deduplicated by trigger ID, scheduled time, and reset generation. Schedule advancement is an idempotent projection of the durable outcome. An interrupted occurrence is not replayed; a recurring schedule may advance while its agent remains review-blocked.

Shutdown stops admission and drains for up to 30 seconds, then records remaining interruptions. Clearing chat history quiesces interactions, queue work, indexing, compaction, reminders, and watcher activity before advancing the reset generation. Generation guards cover durable events and local trigger/log writes. An external request already in progress cannot be undone; its uncertain outcome requires review, and late local callbacks cannot recreate cleared state.

## Lifecycle and rollout controls

Task states are `open`, `waiting_for_user`, `waiting_for_trigger`, `completed`, and `needs_review`. A successful invocation is not task completion. Execution explicitly calls `report_task_state`; a pending draft approval or enabled reminder prevents completion/archival.

Startup and hourly maintenance archive tasks that are explicitly completed and inactive for 30 days. Queued/running work, enabled reminders, pending approval, ambiguous legacy history, and unresolved interruptions protect a task. Archives remain searchable. Accepted continuation restores visibility and reopens the task.

The full settings and initial limits are in `.env.example`. Three independent controls are:

| Variable | Default | Effect when disabled |
|---|---|---|
| `OPENPOKE_AGENT_HYBRID_SEARCH` | `1` | Bounded keyword/reference discovery; no semantic indexing |
| `OPENPOKE_AGENT_COMPACTION` | `1` | Keep bounded recent history and source retrieval without generating new summaries |
| `OPENPOKE_AGENT_AUTO_ARCHIVE` | `1` | Disable automatic archival; explicit lifecycle tools remain available |

Set an optional `Idempotency-Key` header on `/chat/send` to deduplicate retries. The response remains empty HTTP 202 and includes `X-Request-ID`. Reusing the key with different content returns 409. Reset admission returns 503 while quiescing.

Structured logs report candidate counts, local retrieval latency, degraded discovery, index backlog, queue outcomes/wait, archival counts, and provider token usage. `registry_metrics()` exposes local counts for diagnostics. Do not interpret semantic ranking values as probabilities of correct reuse.

## Verification

Install test dependencies and run the offline suite:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

All test and evaluation code lives under `tests/`; the [test file guide](../tests/README.md) describes each file. Tests use isolated stores, mocked model/Gmail behavior, and an autouse guard blocking real HTTP transports. They cover identity conflicts, pagination/context bounds, migration, histories/artifacts, concurrency, restart and interrupted-work review, grouped delivery, trigger deduplication, and reset fencing.

Run the labeled 60-case retrieval evaluation and 1,000/10,000-agent benchmark:

```powershell
python tests/evaluation/evaluate_agent_retrieval.py --benchmark --output server/data/agent_evaluation_report.json
```

The report is written under `server/data/agent_evaluation_report.json`. Offline embeddings are a deterministic synonym-aware test double. Their recall and identity/ambiguity guard results validate retrieval mechanics; they do **not** establish production semantic accuracy or end-to-end model reuse accuracy. The evaluation target is at least 95% recall@30 with no identity-conflict or ambiguity-guard failures.

Live embedding evaluation is explicitly opt-in and incurs OpenRouter usage:

```powershell
python tests/evaluation/evaluate_agent_retrieval.py --live --output server/data/agent_live_evaluation_report.json
```

Live evaluation still uses synthetic registry tasks and does not execute Gmail actions. Review live results before relying on semantic quality in a deployment. Token and latency limits remain configurable; no universal similarity threshold is used.
