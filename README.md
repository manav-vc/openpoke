# OpenPoke: Execution Agent Overload

This fork addresses **Execution Agent Overload** from the [OpenPoke explainer](https://www.shloked.com/openpoke). I kept its core architecture: the **Interaction Agent** talks to the user and delegates work to **Execution Agents**.

## The problem

Originally, the Interaction Agent received the full roster of Execution Agents on every request. As more agents accumulated, unrelated work took up more context. Each Execution Agent also loaded its growing history, and new work could start without a shared concurrency limit.

I treated overload as three connected problems: finding relevant work, loading useful memory, and controlling how much runs at once.

## What changed, and why

### Interaction Agent

The Interaction Agent follows saved task links or searches for relevant Execution Agents. Keyword search catches exact names and identifiers; semantic search finds related wording. It sees at most **30 compact candidates per turn, ten at a time**.

Saved records and embeddings live in SQLite, so the local app does not need a separate vector database.

Search suggests a match; it does not establish one. The Interaction Agent inspects the saved record before continuing it. Stable IDs preserve identity when names change. Conflicting confirmed invoice or case numbers rule out a match: two tasks involving the same person can still be different tasks.

It also searches archived work before creating another Execution Agent. This preserves continuity and reduces duplicates.

### Agent Memory

An Execution Agent receives its task state, a summary of earlier work, and recent history within a fixed budget. It can retrieve original records for exact recipients, amounts, or instructions. New tool results remain stored even when too large for the prompt.

This keeps long-running work manageable without relying on a summary for every detail. Conversation Memory also has a context budget.

### Execution Agents

Execution Agents and Triggers share a persistent queue. By default, **up to four executions run at once**, with up to **100 waiting jobs**. The same Execution Agent cannot run two jobs simultaneously. Related results return together, including failures.

This bounds resource use and prevents simultaneous updates to the same saved work. Executions have a configurable **300-second deadline**. Read-only timeouts leave the task available and permit at most one automatic retry. Attempted writes or unknown tools retain review protection. Blocked submissions return their reason immediately. After a restart, queued work can resume.

Completed Execution Agents archive after 30 inactive days and remain searchable. Pending work, approvals, and active Triggers prevent automatic archival. Inactivity alone does not mean a task is finished.

## How I test it

[Automated tests](tests/README.md) use isolated databases, scripted model responses, and fake Gmail results. Real HTTP calls are blocked. They cover:

- Reusing saved work, rejecting conflicting identities, and handling ambiguous requests.
- Keeping candidate lists and memory bounded while preserving original records.
- Queue limits, grouped results, Triggers, restart recovery, and clearing history.

The [retrieval evaluator](tests/evaluation/evaluate_agent_retrieval.py) runs **60 labeled cases**. Its target is at least **95% recall@30**: the expected Execution Agent should appear among the returned candidates. It also requires no identity-conflict or ambiguity-guard failures. Benchmarks use **1,000 and 10,000 saved Execution Agents** to check candidate counts and local search time.

From the repository root:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
python tests/evaluation/evaluate_agent_retrieval.py --benchmark --output server/data/agent_evaluation_report.json
```

## Is it working well?

Latest offline run, November 14, 2026:

- **129 automated tests passed.**
- **100% recall@30** across the 60 labeled cases, with no identity-conflict or ambiguity-guard failures.
- The candidate pool stayed at **30** in both the 1,000-agent and 10,000-agent benchmarks.

The offline evaluator uses deterministic stand-in embeddings. These checks do not prove that the live Interaction Agent always chooses correctly or that real Gmail workflows succeed. Those still need end-to-end testing. The implementation currently supports one backend process.

## Showing the results

I am considering a before-and-after screenshot comparison. I would run the same request with the same saved work, then compare the chat result alongside the selected Execution Agent, candidate count, and context size from logs or test output. Useful cases are continuing an old task, distinguishing two invoices for the same person, and submitting several tasks together.

This is a proposed demo, not a completed test. I would pair screenshots with automated results to show both visible behavior and the checks behind it.

## Run locally and read more

- [Setup](docs/setup.md)
- [Implementation details and limits](docs/agent-optimization.md)
- [Solution board and sequence diagram](docs/diagrams/README.md)
- [Sequence diagram source](docs/diagrams/openpoke-overload-sequence.mmd)

Built on [OpenPoke](https://github.com/shlokkhemani/OpenPoke). MIT license; see [LICENSE](LICENSE).
