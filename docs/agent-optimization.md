# Agent overload implementation

See the [main README](../README.md) for the problems, solutions, and diagrams. This guide covers the implementation and operating constraints.

## Routing and identity

The Interaction Agent resolves stable EA IDs and saved conversation links first. Otherwise, keyword and semantic search each contribute up to 30 results, merged by reciprocal rank fusion (`k=60`). The IA sees at most 30 compact candidates per turn, 10 at a time, and inspects the current record before dispatching with its version.

Confirmed conflicting invoice/case/task IDs exclude a match; a shared contact or email thread alone does not identify a task. Creating an EA requires searching active and archived records first. Task identifiers become confirmed only through cited tool evidence, not model-generated metadata.

Embeddings use OpenRouter's `openai/text-embedding-3-small`. Normalized vectors are stored in SQLite and searched with a cached NumPy matrix. Missing vectors or provider failures expose degraded search while bounded keyword search continues. A durable outbox retries indexing.

### Why hybrid retrieval

Exact IDs and confirmed references resolve identity before ranking. Keyword search handles literal invoice IDs and names cheaply, but a request such as "beach getaway" need not share the saved task's words. Semantic search handles those paraphrases, but similarity alone cannot establish which of a contact's invoices the user means. Hybrid ranking combines both channels; the same identity and ambiguity guards still apply. The [dense-corpus evaluation](evaluation/README.md) compares the ranking channels separately, including recall in the first 10 candidates the IA sees.

Reciprocal rank fusion (`k=60`) avoids pretending that FTS relevance and vector similarity have comparable score scales. It is a simple starting choice, not a learned or experimentally optimized ranking parameter. The cost is an embedding index, provider dependency, and additional lookup/tool calls. Small rosters can be cheaper to show directly; the paired workflow evaluation reports this overhead instead of assuming a token or latency win at every scale.

## Context and source records

Email drafting runs through an EA. The IA's send_draft tool accepts only saved agent/draft IDs and displays the original successful tool-call content. It is hidden until a linked or inspected task has a pending draft. Queue notifications include saved draft references; presenting a draft preserves its approval requirement.

IA conversation history and EA execution history have separate budgets. Each EA loads structured state, an earlier-work summary, and recent complete exchanges. Full source events and large-result artifacts remain available through scoped history retrieval after compaction. Token counts are estimates with a safety margin.

The default EA history budget is 8,000 estimated tokens, including at most 2,000 for its summary; IA history has a separate 8,000-token budget. A full transcript preserves immediate access but grows without bound. Keeping only recent messages is cheap but can drop earlier corrections. A summary alone can lose exact details. The chosen design keeps a compact working view and retrieves original events when precision matters. The [memory evaluation](evaluation/README.md) checks exact amounts, corrections, identifiers, and pending approval after generating real summaries. It evaluates the combined summary-and-source-retrieval design, not whether a summary alone is lossless.

Thirty candidate cards at about 200 estimated tokens fit the 6,000-token discovery allowance; pages of 10 reduce the initial exposure. Inspections and source reads have their own limits. These are configurable engineering budgets, not measured accuracy optima or exact provider token guarantees. The safety margin defaults to 1.15; tune limits against the deployed model's actual usage and failure rate.

The registry imports legacy roster, history, and trigger ownership idempotently. Stable IDs survive renaming, and ambiguous legacy log collisions are flagged. Previously truncated legacy tool results cannot be reconstructed.

## Queue and recovery

Run **one backend process**. Chat requests and scheduled Triggers share one persistent queue: **100 running jobs, 100 waiting, and one running job per EA**. Independent EAs can run in parallel; requests for the same EA wait their turn. Oldest runnable work starts first. Defaults allow **300 seconds waiting** and **300 seconds running**. Set `OPENPOKE_AGENT_MAX_CONCURRENCY` to change the global limit.

A semaphore would cap concurrent coroutines with less code, but its waiters, ownership, and undelivered results disappear on restart. The persistent queue also records admission, per-agent serialization, deadlines, grouped delivery, and uncertain effects. Its tradeoffs are more recovery logic, SQLite writes, and a single-process deployment constraint.

The concurrency ceiling of 100 matches the requested submission configuration. It is not a claim that a particular model provider or machine sustains 100 live requests. The [admission chart](charts/README.md) verifies bounded admission using simulated jobs; it does not establish real throughput. Waiting capacity 100 and 300-second deadlines bound backlog and stalled work. Start lower when provider rate limits or memory require it, then tune from actual queue wait, failure rate, and latency measurements.

Each result group is sealed when submissions finish and delivered after every job has an outcome, including failures or rejections. Workers can start before sealing. Each initiating request permits **5 follow-up stages, 20 submissions, and at most one automatic retry of eligible interrupted read-only work**.

After restart, unexpired queued work resumes. Interrupted writes or unknown tools require review; new submissions for that EA are rejected and existing queued follow-ups are held. Inspect the blocking execution and original tool state, then use `resolve_interrupted_execution` to mark it completed, abandon it, or explicitly retry. Uncertain actions are not replayed automatically.

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

Use [tests/README.md](../tests/README.md) for coverage and evaluation commands. **196 offline tests pass.** Real-model evaluations check IA/EA workflows against upstream, retrieval among 288 similar tasks, and source-assisted recall after real compaction. [Recorded results](evaluation/README.md) preserve the original 9/10 unavailable-email failure, its runtime fix and later outage verification, the dense retrieval ablations, and the 4/4 memory run at the submitted budgets. Real Gmail workflows remain unvalidated.
