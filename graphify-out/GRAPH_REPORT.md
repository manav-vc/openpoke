# Graph Report - openpoke  (2026-11-14)

## Corpus Check
- 117 files · ~68,606 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1186 nodes · 2879 edges · 58 communities (43 shown, 12 thin omitted)
- Extraction: 97% EXTRACTED · 3% INFERRED · 0% AMBIGUOUS · INFERRED: 93 edges (avg confidence: 0.91)
- Token cost: 0 model tokens for this deterministic cleanup refresh. Token counts for the prior host-agent semantic extraction remain unavailable.

## Community Hubs (Navigation)
- Reminder Triggers and Scheduling
- Execution Transcript Storage
- Conversation Event Logging
- Execution Services and Configuration
- History Context and Budgets
- Conversation Summary Pipeline
- Execution Tool Registration
- Interaction Agent Runtime
- Task Routing and Identity Tests
- Email Monitoring and Processing
- Conversation Working Memory
- OpenRouter and Email Classification
- Execution Agent Prompt Construction
- Retrieval Index and Maintenance
- Chat Page and Presentation
- Gmail Tools and Integration
- Email Search Tool Schemas
- Task Dispatch Tool Context
- User Timezone Storage
- Chat Message Rendering
- Email Search Result Conversion
- Agent Roster Compatibility
- Task Retrieval and Evaluation
- Chat Input and Scrolling
- Chat API Proxy
- Settings Modal and Identity
- Task Registry and Identity
- Backend Routes and Models
- Durable Execution Queue
- Execution Effects and Recovery
- Execution Queue Tests
- Application Startup and Reset
- Agentic Email Search Loop
- Interaction Recovery and Integration
- Backend Configuration and CLI
- Email Search Input Validation
- Frontend Root Layout
- Offline Test Network Guard
- Chat History API Proxy
- Gmail Connection API Proxy
- Gmail Disconnection API Proxy
- Gmail Status API Proxy
- Timezone API Proxy
- Architecture Documentation and Instructions
- TypeScript Compiler Configuration
- Frontend Build Dependencies
- Frontend Development Dependencies
- Frontend Runtime Dependencies
- Frontend Package Scripts
- Root Next Type Declarations
- Agent Package Metadata
- Retrieval Evaluation Package
- Automated Test Package
- Frontend Next Type Declarations
- Overload Solution Design

## God Nodes (most connected - your core abstractions)
1. `ExecutionService` - 58 edges
2. `AgentRegistry` - 52 edges
3. `get_settings()` - 44 edges
4. `ExecutionQueueTests` - 40 edges
5. `SearchService` - 38 edges
6. `get_registry()` - 31 edges
7. `ExecutionAgentRuntime` - 30 edges
8. `estimate_tokens()` - 30 edges
9. `InteractionAgentRuntime` - 28 edges
10. `ConversationLog` - 27 edges

## Surprising Connections (you probably didn't know these)
- `state()` --uses--> `ConversationLog`  [INFERRED]
  tests/test_runtime_integration.py → server/services/conversation/log.py
- `services()` --uses--> `WorkingMemoryLog`  [INFERRED]
  tests/test_interaction_routing.py → server/services/conversation/summarization/working_memory_log.py
- `state()` --uses--> `WorkingMemoryLog`  [INFERRED]
  tests/test_runtime_integration.py → server/services/conversation/summarization/working_memory_log.py
- `test_lossless_large_tool_artifact_and_bounded_source_read()` --calls--> `HistoryService`  [EXTRACTED]
  tests/test_registry_history.py → server/services/execution/history.py
- `ExecutionQueueTests` --uses--> `ExecutionService`  [INFERRED]
  tests/test_execution_queue.py → server/services/execution/queue.py

## Import Cycles
- 4-file cycle: `server/services/conversation/log.py -> server/services/conversation/summarization/__init__.py -> server/services/conversation/summarization/scheduler.py -> server/services/conversation/summarization/summarizer.py -> server/services/conversation/log.py`

## Communities (58 total, 12 thin omitted)

### Community 0 - "Reminder Triggers and Scheduling"
Cohesion: 0.05
Nodes (45): TriggerScheduler, TriggerRecord, TriggerService, TriggerStore, Worker, generation_write_guard(), _isoformat(), _utc_now() (+37 more)

### Community 10 - "Execution Transcript Storage"
Cohesion: 0.07
Nodes (23): ExecutionAgentLogStore, _decode_payload(), _encode_payload(), _slugify(), Lock, Path, Record an incoming request from the interaction agent., Record an agent action (tool call). (+15 more)

### Community 11 - "Conversation Event Logging"
Cohesion: 0.09
Nodes (11): ConversationLog, TranscriptFormatter, FakeQueue, _decode_payload(), services(), Protocol, Path, fixture (+3 more)

### Community 12 - "Execution Services and Configuration"
Cohesion: 0.11
Nodes (16): ExecutionBatchManager, ExecutionResult, get_settings(), get_execution_service(), get_registry(), utc_now(), Compatibility facade over the shared durable execution service. New callers…, Simplified Execution Agent Runtime. (+8 more)

### Community 14 - "History Context and Budgets"
Cohesion: 0.16
Nodes (17): HistoryService, bounded_json(), _encoding(), estimate_tokens(), truncate_text(), test_json_excerpt_remains_valid_with_escaping_and_unicode(), test_small_values_preserve_structure_and_empty_budgets_are_safe(), test_text_excerpt_is_bounded_and_original_is_unchanged() (+9 more)

### Community 15 - "Conversation Summary Pipeline"
Cohesion: 0.17
Nodes (19): SummaryPrompt, LogEntry, _default_formatter(), _encode_payload(), _resolve_working_memory_log(), build_summarization_prompt(), _format_existing_summary(), _format_log_entries() (+11 more)

### Community 17 - "Execution Tool Registration"
Cohesion: 0.13
Nodes (18): PendingExecution, get_task_registry(), get_task_schemas(), build_registry(), get_tool_registry(), get_tool_schemas(), Any, Any (+10 more)

### Community 18 - "Interaction Agent Runtime"
Cohesion: 0.13
Nodes (12): InteractionAgentRuntime, InteractionResult, ToolResult, build_system_prompt(), prepare_message_with_history(), _interaction_lock(), test_committed_plan_resumes_after_cancel_without_repeating_job(), test_idempotency_key_rejects_different_message() (+4 more)

### Community 19 - "Task Routing and Identity Tests"
Cohesion: 0.19
Nodes (17): FakeSearch, explicit_references(), handle_tool_call(), context(), test_ambiguous_exact_matches_require_user_clarification(), test_conflicting_identity_is_rejected_before_dispatch(), test_creation_requires_archive_search_and_inspection(), test_degraded_discovery_is_not_a_no_match_signal() (+9 more)

### Community 2 - "Email Monitoring and Processing"
Cohesion: 0.06
Nodes (34): ImportantEmailWatcher, EmailTextCleaner, ProcessedEmail, GmailSeenStore, _resolve_interaction_runtime(), build_processed_email(), build_processed_emails(), parse_gmail_fetch_response() (+26 more)

### Community 22 - "Conversation Working Memory"
Cohesion: 0.17
Nodes (9): SummaryState, WorkingMemoryLog, _current_timestamp(), _decode_payload(), _encode_payload(), _format_line(), Path, Persisted working-memory summary state. (+1 more)

### Community 23 - "OpenRouter and Email Classification"
Cohesion: 0.18
Nodes (16): OpenRouterError, _build_messages(), _handle_response_error(), _headers(), request_chat_completion(), classify_email_importance(), _coerce_arguments(), _format_email_payload() (+8 more)

### Community 27 - "Execution Agent Prompt Construction"
Cohesion: 0.15
Nodes (8): ExecutionAgent, Build message array for LLM call. Args: current_instruction: Current…, Record agent's response to the log., Record tool execution details., Manages state and history for an execution agent., Initialize an execution agent. Args: name: Human-readable agent name (e.g.,…, Build the system prompt for this agent., Build system prompt including agent history. Returns: System prompt with…

### Community 28 - "Retrieval Index and Maintenance"
Cohesion: 0.19
Nodes (5): AgentMaintenance, get_search_service(), _utc_now(), Bounded hybrid discovery over durable agent records. Identity evidence and…, Process durable index work, checking both generation and content version.

### Community 29 - "Chat Page and Presentation"
Cohesion: 0.26
Nodes (10): ChatHeaderProps, ErrorBannerProps, formatEscapeCharacters(), isRenderableMessage(), Page(), toBubbles(), ChatHeader(), ErrorBanner() (+2 more)

### Community 3 - "Gmail Tools and Integration"
Cohesion: 0.08
Nodes (58): Settings, GmailConnectPayload, GmailDisconnectPayload, GmailStatusPayload, gmail_fetch_emails(), build_registry(), _execute(), get_schemas() (+50 more)

### Community 31 - "Email Search Tool Schemas"
Cohesion: 0.27
Nodes (8): TaskEmailSearchPayload, get_completion_schema(), get_schemas(), Any, Email search task package., Schemas for the email search task tools., Return the JSON schema for the email search task., Envelope for the final email selection.

### Community 34 - "User Timezone Storage"
Cohesion: 0.28
Nodes (3): TimezoneStore, Path, Stores a single timezone string supplied by the client UI.

### Community 36 - "Chat Message Rendering"
Cohesion: 0.32
Nodes (4): ChatMessagesProps, ChatBubble, ChatMessages(), clsx

### Community 37 - "Email Search Result Conversion"
Cohesion: 0.32
Nodes (8): EmailSearchToolResult, GmailSearchEmail, _perform_search(), _processed_to_schema(), BaseModel, Clean email representation with enhanced content processing., Structured payload for each tool-call response., Convert shared processed email into GmailSearchEmail schema.

### Community 4 - "Task Retrieval and Evaluation"
Cohesion: 0.08
Nodes (45): OpenRouterEmbedder, SearchService, OfflineSemanticEmbedder, FakeEmbedder, _identity_resolution(), _normalized(), reference_evidence(), _reference_value() (+37 more)

### Community 40 - "Chat Input and Scrolling"
Cohesion: 0.33
Nodes (4): ChatInputProps, AutoScrollOptions, ChatInput(), react

### Community 41 - "Chat API Proxy"
Cohesion: 0.40
Nodes (5): UIMessage, UIMsgPart, POST(), uiToOpenAIContent(), runtime

### Community 42 - "Settings Modal and Identity"
Cohesion: 0.60
Nodes (4): Settings, coerceEmailFrom(), deriveEmailFromPayload(), SettingsModal()

### Community 5 - "Task Registry and Identity"
Cohesion: 0.07
Nodes (23): AgentRegistry, DuplicateAgentError, StaleAgentError, StaleGenerationError, json_dump(), test_reset_serializes_with_trigger_write_inside_generation_guard(), registry(), test_journal_dedup_generation_and_reset() (+15 more)

### Community 6 - "Backend Routes and Models"
Cohesion: 0.07
Nodes (45): ChatHistoryClearResponse, ChatHistoryResponse, ChatMessage, ChatRequest, HealthResponse, RootResponse, SetTimezoneRequest, SetTimezoneResponse (+37 more)

### Community 7 - "Durable Execution Queue"
Cohesion: 0.10
Nodes (8): ExecutionService, _json(), test_coordinated_reset_cancels_owned_work_and_fences_old_callbacks(), test_real_registry_queue_discovers_and_reuses_same_agent(), test_startup_recovers_accepted_user_turn(), Classify dispatched tools, never the model's description of its task. Old jobs…, Correct a legacy timeout only when its journal proves safe reads., Expose review IDs without loading another agent's execution history.

### Community 8 - "Execution Effects and Recovery"
Cohesion: 0.08
Nodes (28): ExecutionAgentRuntime, UncertainExecutionError, tool_effect(), setup_runtime(), test_all_exposed_execution_tools_have_an_audited_effect(), test_confirmed_identity_requires_successful_original_source_not_echo(), test_created_draft_preserves_sources_and_cannot_be_completed_before_approval(), test_error_response_after_mutation_stops_execution() (+20 more)

### Community 9 - "Execution Queue Tests"
Cohesion: 0.16
Nodes (3): ExecutionQueueTests, until(), Check queue limits, ordering, timeouts, retries, and recovery without live APIs.

### Community 13 - "Application Startup and Reset"
Cohesion: 0.13
Nodes (24): quiesce_interactions(), _start_trigger_scheduler(), _stop_trigger_scheduler(), get_conversation_log(), resume_summarization(), stop_summarization(), registry_metrics(), reset_all() (+16 more)

### Community 16 - "Agentic Email Search Loop"
Cohesion: 0.14
Nodes (23): get_system_prompt(), _build_response(), _create_error_response(), _create_success_response(), _execute_tool_calls(), _extract_assistant_message(), _handle_completion_tool(), _parse_arguments() (+15 more)

### Community 21 - "Interaction Recovery and Integration"
Cohesion: 0.12
Nodes (14): recover_pending_interactions(), resume_interactions(), track_interaction(), get_tool_schemas(), configure_logging(), test_quiesced_interactions_reject_new_acceptance(), state(), fixture (+6 more)

### Community 32 - "Backend Configuration and CLI"
Cohesion: 0.20
Nodes (5): _load_env_file(), main(), Simplified configuration management., Load .env from root directory if present., CLI entrypoint for running the FastAPI app with Uvicorn.

### Community 38 - "Email Search Input Validation"
Cohesion: 0.25
Nodes (8): task_email_search(), _validate_gmail_connection(), _validate_openrouter_config(), _validate_search_query(), Validate search query and return error message if invalid., Validate Gmail connection and return user ID or None., Validate OpenRouter configuration and return (api_key, model) or (None, error)., Run an agentic Gmail search for the provided query.

### Community 46 - "Offline Test Network Guard"
Cohesion: 0.50
Nodes (3): prevent_external_http(), fixture, Configure offline test defaults and block real HTTP requests.

### Community 47 - "Chat History API Proxy"
Cohesion: 0.83
Nodes (3): DELETE(), forward(), GET()

### Community 1 - "Architecture Documentation and Instructions"
Cohesion: 0.06
Nodes (68): __dirname, nextConfig, repoRoot, Labeled retrieval cases, One automatic read-only retry, Sealed result groups, Opt-in live embedding evaluation, report_task_state (+60 more)

### Community 20 - "TypeScript Compiler Configuration"
Cohesion: 0.09
Nodes (21): compilerOptions, allowJs, baseUrl, esModuleInterop, incremental, isolatedModules, jsx, lib (+13 more)

### Community 25 - "Frontend Build Dependencies"
Cohesion: 0.13
Nodes (13): name, private, version, config, autoprefixer, eslint, eslint-config-next, postcss (+5 more)

### Community 35 - "Frontend Development Dependencies"
Cohesion: 0.22
Nodes (9): devDependencies, autoprefixer, eslint, eslint-config-next, postcss, tailwindcss, @types/node, @types/react (+1 more)

### Community 43 - "Frontend Runtime Dependencies"
Cohesion: 0.40
Nodes (5): dependencies, clsx, next, react, react-dom

### Community 44 - "Frontend Package Scripts"
Cohesion: 0.40
Nodes (5): scripts, build, dev, lint, start

### Community 24 - "Overload Solution Design"
Cohesion: 0.15
Nodes (16): Evidence and practical limits, Agent overload: the solutions, Agent overload, Archive completed work safely, Bound the conversation context, Check identity before continuing, Find and reuse work, Keep context useful (+8 more)

## Knowledge Gaps
- **64 isolated node(s):** `runtime`, `UIMsgPart`, `UIMessage`, `runtime`, `runtime` (+59 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 357 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **12 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `AgentRegistry` connect `Task Registry and Identity` to `Task Retrieval and Evaluation`, `Agent Roster Compatibility`, `Execution Effects and Recovery`, `Execution Queue Tests`, `Conversation Event Logging`, `Execution Services and Configuration`, `History Context and Budgets`, `Task Routing and Identity Tests`, `Interaction Recovery and Integration`?**
  _High betweenness centrality (0.121) - this node is a cross-community bridge._
- **Why does `ExecutionService` connect `Durable Execution Queue` to `Reminder Triggers and Scheduling`, `Task Registry and Identity`, `Execution Effects and Recovery`, `Execution Queue Tests`, `Execution Services and Configuration`, `Interaction Agent Runtime`, `Task Routing and Identity Tests`, `Interaction Recovery and Integration`?**
  _High betweenness centrality (0.076) - this node is a cross-community bridge._
- **Why does `get_settings()` connect `Execution Services and Configuration` to `Backend Configuration and CLI`, `Task Dispatch Tool Context`, `Gmail Tools and Integration`, `Task Retrieval and Evaluation`, `Email Search Input Validation`, `Backend Routes and Models`, `Conversation Event Logging`, `Application Startup and Reset`, `History Context and Budgets`, `Conversation Summary Pipeline`, `Agentic Email Search Loop`, `Execution Tool Registration`, `Interaction Agent Runtime`, `Interaction Recovery and Integration`, `OpenRouter and Email Classification`, `Retrieval Index and Maintenance`?**
  _High betweenness centrality (0.063) - this node is a cross-community bridge._
- **Are the 4 inferred relationships involving `ExecutionService` (e.g. with `ExecutionAgentRuntime` and `InteractionAgentRuntime`) actually correct?**
  _`ExecutionService` has 4 INFERRED edges - model-reasoned connections that need verification._
- **Are the 8 inferred relationships involving `AgentRegistry` (e.g. with `HistoryService` and `AgentRoster`) actually correct?**
  _`AgentRegistry` has 8 INFERRED edges - model-reasoned connections that need verification._
- **Are the 2 inferred relationships involving `ExecutionQueueTests` (e.g. with `ExecutionService` and `AgentRegistry`) actually correct?**
  _`ExecutionQueueTests` has 2 INFERRED edges - model-reasoned connections that need verification._
- **What connects `runtime`, `UIMsgPart`, `UIMessage` to the rest of the system?**
  _64 weakly-connected nodes found - possible documentation gaps or missing edges._
## Refresh verification

- Source coverage: 117 supported files, with SHA-256 hashes verified; current working-tree edits are included.
- Removed historical diagram nodes and updated documentation references and source locations after approved cleanup.
- The remaining sequence diagram is linked directly from the main README and recorded separately in `source_snapshot.json`, because `.mmd` is not a supported extraction format.
- Semantic coverage: 9 documents and 1 current image. Two JSON source/fixture files are inventoried without semantic extraction; see `inventory_only_files`.
- Graph health warning: 248 unresolved endpoint edges and 181 same-pair combinations in the undirected view; 0 self-loops. Most unresolved endpoints are dependency imports; some package/CSS imports are also unresolved.
- Full relationships, including unresolved references, remain in `extraction.json`; detailed diagnostics are in `health.json`. All exported graph edges have valid endpoints.
- Query memories, caches, backups, and machine-specific graphify state stay local and are ignored by Git.
