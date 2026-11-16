# Before and after: agent overload

**Recorded evidence:** the concurrency chart uses a **four-worker fixture**, while the current application default is **100 concurrent executions**. Its measured values and raw JSON are preserved. A new measurement of the 100-worker configuration is **pending**; these charts do not claim it has passed.

[Validation status](../evaluation/README.md#validation-status) · [Source measurements](measurements.json) · [Reproduction](#sources-and-reproduction)

Three charts correspond to the three problems in the [Excalidraw presentation](https://link.excalidraw.com/l/2C1urTzZVM0/2hmrnSZUcZZ). Put each PNG beside its matching problem and solution. The SVG copies scale cleanly for slides or further editing.

| Problem | Before | After | Evidence |
| --- | --- | --- | --- |
| IA roster exposure at 10,000 saved EAs | 10,000 roster entries | At most 30 search candidates, 10 per page | Upstream renderer + saved offline retrieval benchmark |
| EA history at 100 completed exchanges | 59,459 estimated context tokens | 4,529 estimated context tokens | Actual context builders on synthetic history |
| Simultaneous EA work in a 24-job burst | 24 running | 4 running at the fixture's explicit limit; the rest wait | Source-derived upstream schedule + production queue admission replay |

## 1. IA roster exposure

![Full roster entries versus bounded search candidates](01-roster-context.png)

**Explain it:** “As the number of saved EAs grows, the IA searches a bounded set of candidates instead of receiving the entire roster.”

The before values come from the unmodified upstream `_render_active_agents` function. The after values come from the existing offline benchmark: 12 repeats each at 1,000 and 10,000 agents. Counts compare roster entries with search candidates; they do not compare equal-sized records or total prompt tokens. Recent task links and other IA context are outside this chart. Search still scans more stored data at larger sizes; this chart makes no retrieval-speed claim.

[SVG](01-roster-context.svg)

## 2. EA history context

![Growing saved history versus bounded execution context](02-history-context.png)

**Explain it:** “We retain the original records in storage while giving each run a bounded context, with exact source details available on demand.”

The same 100 synthetic invoice exchanges go through the upstream logger and the new journal. Each exchange contains one request, one tool call with a 60-line result, and one response. The upstream logger natively truncates tool results to 500 characters; that behavior is preserved. Its default context builder then loads all logged exchanges.

The new context builder includes task identity and the two most recent completed exchanges. This fixture generates no earlier-work summary and does not test summary quality. All 100 full tool results are verified to remain in the new journal. The 8,000-token line is the configured context budget; measured context need not fill it. Static system instructions and the next incoming request are excluded.

These are conservative character-based estimates with a 15% safety margin, using the application's offline fallback. They are not provider-reported tokens. A rerun with cached `cl100k_base` available will record that tokenizer and may produce different values. The fixture is an illustration of context growth, not a production token, cost, or accuracy estimate.

[SVG](02-history-context.svg)

## 3. Execution concurrency — recorded four-worker fixture

![Burst execution versus a shared queue with four running jobs](03-execution-concurrency.png)

**Explain it:** “In this recorded four-worker fixture, a burst becomes queued work: four EAs run together and the remaining jobs wait.”

Twenty-four independent requests, each for a distinct EA, arrive at simulated time zero. Each job lasts exactly one simulated second. The upstream trace is derived from its immediate-execution code. The after trace uses the production `submit` and `_claim_next` SQLite admission paths, with simulated completions. All 24 jobs complete; peak admission is four. The measurement script explicitly sets this fixture limit; it does not read the application's updated 100-worker default. Distinct requests avoid changing the default 20-submission limit per initiating request.

This is an admission simulation, not an end-to-end scheduler, live-model latency, or throughput benchmark. It deliberately shows the tradeoff: spreading work across six simulated seconds instead of a one-second burst. Per-EA serialization, queue overflow, timeouts, and restart recovery belong to separate tests.

[SVG](03-execution-concurrency.svg)

## Sources and reproduction

The baseline is upstream commit `5b5f635935a64ab37884c025d70abb0ed731c094`. [measurements.json](measurements.json) contains every plotted point, the saved retrieval benchmark excerpt, source hashes, and fixture details. It is safe to share: all fixture data is synthetic.

From the repository root, using the project Python environment:

```powershell
python tests/evaluation/measure_overload.py
python -m pip install matplotlib
python tests/evaluation/render_overload_charts.py
```

Measurement uses temporary SQLite databases and logs, with no model or Gmail calls. The saved retrieval report is used when available; otherwise the committed benchmark excerpt is reused. PNG and SVG files are regenerated from the JSON. These charts are separate from the real-model workflow evaluation and do not present its results as a before/after comparison.
