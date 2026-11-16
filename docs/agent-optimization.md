# Agent overload implementation

See the [main README](../README.md) for the problems, solutions, and diagrams. This guide covers the implementation and operating constraints.

## Routing and identity

The Interaction Agent resolves stable EA IDs and saved conversation links first. Otherwise, keyword and semantic search each contribute up to 30 results, merged by reciprocal rank fusion (`k=60`). The IA sees at most 30 compact candidates per turn, 10 at a time, and inspects the current record before dispatching with its version.

Confirmed conflicting invoice/case/task IDs exclude a match; a shared contact or email thread alone does not identify a task. Creating an EA requires searching active and archived records first. Task identifiers become confirmed only through cited tool evidence, not model-generated metadata.

Embeddings use OpenRouter's `openai/text-embedding-3-small`. Normalized vectors are stored in SQLite and searched with a cached NumPy matrix. Missing vectors or provider failures expose degraded search while bounded keyword search continues. A durable outbox retries indexing.

## Context and source records

Email drafting runs through an EA. The IA's send_draft tool accepts only saved agent/draft IDs and displays the original successful tool-call content. It is hidden until a linked or inspected task has a pending draft. Queue notifications include saved draft references; presenting a draft preserves its approval requirement.

IA conversation history and EA execution history have separate budgets. Each EA loads structured state, an earlier-work summary, and recent complete exchanges. Full source events and large-result artifacts remain available through scoped history retrieval after compaction. Token counts are estimates with a safety margin.

The registry imports legacy roster, history, and trigger ownership idempotently. Stable IDs survive renaming, and ambiguous legacy log collisions are flagged. Previously truncated legacy tool results cannot be reconstructed.

## Queue and recovery

Run **one backend process**. Chat requests and scheduled Triggers share one persistent queue: **100 running jobs, 100 waiting, and one running job per EA**. Oldest runnable work starts first. Defaults allow **300 seconds waiting** and **300 seconds running**.

Each result group is sealed when submissions finish and delivered after every job has an outcome, including failures or rejections. Workers can start before sealing. Each initiating request permits **5 follow-up stages, 20 submissions, and at most one automatic retry of eligible interrupted read-only work**.

After restart, unexpired queued work resumes. An uncertain external write stops a live execution immediately, before another tool call or model turn. Interrupted writes or unknown tools require review; new submissions for that EA are rejected and existing queued follow-ups are held. Inspect the blocking execution and original tool state, then use `resolve_interrupted_execution` to mark it completed, abandon it, or explicitly retry. Uncertain actions are not replayed automatically.

Interaction plans are journaled before effects. Stable operation keys deduplicate submissions and replies, while trigger ID, scheduled time, and reset generation deduplicate reminder occurrences. Shutdown drains for up to 30 seconds before recording interruptions. Clearing history stops background work and advances a generation guard; an external action already in progress may still require review.

## Storage and lifecycle

The default store is `server/data/agents/registry.db`. SQLite holds EA records, FTS5 search, vectors, source history, summaries, jobs, result groups, and recovery journals. **FTS5 support is required; no separate vector database is needed.**

EA states are `open`, `waiting_for_user`, `waiting_for_trigger`, `completed`, and `needs_review`. Execution reports task state explicitly; a successful run alone does not complete a task.

Startup and hourly maintenance archive completed EAs after 30 inactive days. Queued/running work, active Triggers, pending approval, ambiguous legacy history, or unresolved interruptions prevent archival. Archived EAs remain searchable and reopen when continued.

## Configuration and API

[.env.example](../.env.example) lists all settings and default limits. These controls can be disabled independently:

| Variable | Default | Behavior when disabled |
| --- | --- | --- |
| `OPENPOKE_AGENT_HYBRID_SEARCH` | `1` | Bounded keyword/reference discovery; no semantic indexing. |
| `OPENPOKE_AGENT_COMPACTION` | `1` | Bounded recent history and source retrieval; no new summaries. |
| `OPENPOKE_AGENT_AUTO_ARCHIVE` | `1` | Explicit lifecycle tools remain available; no automatic archival. |

`POST /chat/send` keeps the HTTP 202/poll-history flow and returns `X-Request-ID`. An optional `Idempotency-Key` deduplicates retries; reusing it with different content returns 409. Admission returns 503 while a history reset is quiescing.

Structured logs and `registry_metrics()` expose retrieval, indexing, queue, archival, and token-usage diagnostics. Ranking scores are not probabilities of a correct match.

## Verification

The current 100-concurrent default is reflected in Settings, the environment example, and both queue fallbacks. Saved workflow runs and the four-worker admission fixture predate this update; runtime checks on this revision are **pending**. See the [validation status guide](evaluation/README.md#validation-status).


Use [tests/README.md](../tests/README.md) for test coverage and evaluation commands. Offline stand-ins validate retrieval and recovery mechanics. The real-model workflow evaluator checks IA/EA behavior, hybrid search, queue delivery, and synthetic actions. [Recorded full runs](evaluation/README.md) passed 10/10 turns earlier and 9/10 in the latest run, with identical evaluated application code and fixtures. The latest unavailable-email case reported the outage correctly but failed the queue-outcome check because the job ended as `interrupted`. Real Gmail workflows remain unvalidated.
