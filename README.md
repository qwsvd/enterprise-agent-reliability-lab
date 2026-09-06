# Enterprise Agent Reliability Lab

This repository contains a typed local after-sales backend, a small LLM tool-calling agent, official-SDK MCP integration, repository Agent Skills, provider-neutral reliability controls, OpenTelemetry tracing, deterministic automated Agent evaluations, and an external benchmark integration boundary. It is built with FastAPI, Pydantic, SQLAlchemy, SQLite, HTTPX, MCP Python SDK v2, OpenTelemetry, and the open `SKILL.md` convention. RAG and later phases remain out of scope.

## Setup and run

Requires Python 3.11+.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -e ".[dev]"
uvicorn app.main:app --reload
```

The default database is `sqlite:///./after_sales.db`. Override it with `DATABASE_URL` as shown in `.env.example`. API docs are at `http://127.0.0.1:8000/docs`.

## Demo data and API

Startup idempotently seeds customer `CUS-001`, order `ORD-1024` for `299.00 CNY`, and its shipment delayed by 10 days.

- `GET /health`
- `GET /customers/{customer_code}`
- `GET /orders/{order_code}`
- `GET /orders/{order_code}/shipping`
- `GET /policies/refund`
- `POST /refunds` (requires `Idempotency-Key` header)
- `POST /tickets`

Shipments delayed at least 7 days may qualify. Orders can only be refunded once; the refund cannot exceed the order amount; and refunds above 1000 CNY require human approval. An identical idempotent retry returns the original refund, while reusing a key for another request is rejected.

## Phase 2 architecture

The agent implementation is split into small boundaries:

- `app/agent/runtime.py`: bounded model/tool loop and conversation state
- `app/agent/providers.py`: provider protocol, optional OpenAI-compatible client, and deterministic scripted provider
- `app/agent/tools.py`: JSON schemas, Pydantic argument validation, tool dispatch, and structured errors
- `app/agent/types.py`: typed model responses, tool calls, events, and run results
- `app/services.py`: shared Phase 1 business rules used by both HTTP routes and agent tools

The six available tools are `get_customer`, `get_order`, `get_shipping`, `get_refund_policy`, `create_refund`, and `create_support_ticket`.

The representative workflow is:

> The customer says order ORD-1024 has still not arrived after 10 days. Check the relevant customer, order, shipment and refund policy. If the order is eligible, issue the appropriate refund and create a support ticket. Then report what happened.

The model decides which tools to call. The runtime validates and executes each call, adds structured results to the conversation, and continues until the model answers or the step limit is reached. Refund writes still go through the Phase 1 service, including eligibility, duplicate, amount, idempotency, and pending-approval behavior.

## Phase 3 MCP architecture

Phase 2 executes `ToolRegistry` directly. Phase 3 adds a protocol boundary without replacing that local path:

- `app/mcp_integration/server.py` uses the official SDK v2 `MCPServer` and exposes the same six typed business tools.
- MCP handlers delegate to the existing `ToolRegistry`, which delegates writes and policy decisions to the Phase 1 service layer.
- `app/mcp_integration/client.py` uses the official high-level `Client` to discover tools with `tools/list` and invoke them with `tools/call`.
- `MCPToolAdapter` converts the discovered names, descriptions, and input schemas into the existing AgentRuntime tool interface. It does not hard-code the six schemas.
- Tests use the SDK's in-process client/server connection. The local demo uses the SDK's stdio transport and negotiates protocol behavior through the SDK.

```text
User task
  -> AgentRuntime
  -> MCP-discovered tool schema
  -> MCP client
  -> MCP server
  -> existing Phase 1 ToolRegistry/service layer
  -> SQLite database
  -> structured MCP result
  -> AgentRuntime
```

Start the stdio MCP server for a local MCP client or inspector:

```bash
after-sales-mcp-server
```

Run the fully local deterministic MCP Agent demonstration. It launches the real MCP server over stdio, uses `ScriptedProvider` instead of a paid model, and creates a temporary database unless `--database-url` is supplied:

```bash
after-sales-mcp-demo
```

The integration is tested against this repository's server and official SDK client paths. Interoperability with external MCP hosts is not claimed.

## Phase 4 Agent Skills

An Agent Skill is a version-controlled, reusable workflow. Repository skills live at `.agents/skills/<skill-name>/SKILL.md` and use YAML frontmatter followed by concise procedural instructions:

```markdown
---
name: example-skill
description: Explain when this workflow should be selected.
---

Workflow instructions...
```

`SkillRegistry` discovers and validates only lightweight `name`, `description`, and file-location metadata at first. `SkillAwareTools` adds a `load_skill` capability to the existing tool set. Only when the model selects that capability does the registry read the selected complete `SKILL.md`; the resulting instructions remain in the conversation for the rest of that run. Other skill bodies are not preloaded.

The repository includes:

- `delayed-order-resolution`: inspect a delayed order, consult authoritative policy, use refund and ticket tools, and report the exact outcome.
- `high-value-refund-escalation`: preserve human-approval behavior and distinguish pending, approved, rejected, and failed states.

The boundaries are deliberate:

- **Skill:** reusable workflow and instructions.
- **Tool:** executable capability such as looking up an order or creating a refund.
- **MCP:** protocol and discovery boundary for those executable capabilities.
- **Service layer:** authoritative business rules and state changes.

Skills do not contain executable refund thresholds, mutate the database, or replace service validation.

```text
User task
  -> AgentRuntime
  -> skill metadata catalog
  -> selected SKILL.md
  -> skill-guided reasoning
  -> MCP-discovered business tools
  -> MCP server
  -> existing service layer
  -> SQLite database
  -> tool results
  -> AgentRuntime
  -> final response
```

Run the deterministic Agent + Skill + in-process MCP demonstration:

```bash
after-sales-skills-demo
```

The demo uses `ScriptedProvider`, loads `delayed-order-resolution` through `load_skill`, discovers business tools from the MCP server, and uses a temporary SQLite database. It requires no API key or external network.

## Phase 5 reliability controls

Reliability controls are runtime execution guards and recovery policy. They make failure behavior bounded and inspectable without moving refund or ticket rules out of the service layer. `ReliabilityConfig` supplies conservative defaults and can be replaced per run.

- **Execution budgets:** defaults allow 8 Agent steps, 12 provider calls, and 32 actual tool executions. Provider and tool retries consume their corresponding call budget. The runtime checks a budget before every attempt and returns the exact exhausted-budget reason without making the next call.
- **Provider retry:** only `ProviderError` values explicitly marked retryable are retried. The default is at most 3 attempts with bounded exponential backoff. Permanent authentication, request, or malformed-response failures stop immediately.
- **Timeouts:** the default provider and tool deadlines are 30 seconds. The runtime passes the provider deadline through `LLMProvider`; the OpenAI-compatible provider applies it to HTTPX. It passes the tool deadline to the MCP adapter, which applies an AnyIO cancellation scope. Timeout results are typed and never silently ignored.
- **Tool retry:** retry requires an explicit `retryable: true` error. Validation errors, unknown tools, malformed results, skill-loading errors, business-rule rejection, and MCP tool rejection are permanent. Read-only calls may retry; idempotency-keyed `create_refund` may retry because the existing service provides a strong guarantee.
- **Side-effect safety:** `create_support_ticket` has no idempotency contract. An ambiguous retryable failure therefore terminates as `unsafe_retry_blocked`; the runtime never replays it automatically. No ticket idempotency was invented for this phase.
- **Loop protection:** after 3 successful, validated executions of the same canonical tool name and arguments, the next identical request is blocked as `repeated_tool_call`. The threshold is configurable and independent of the step budget.
- **Failure budget:** 3 consecutive failed provider/tool attempts terminate by default. Any successful operation resets the consecutive count. Total failure records remain available in the result.

`AgentResult` retains the existing status, response, steps, and tool events while adding `termination_reason`, `model_calls`, `tool_calls`, `retries`, `failures`, `consecutive_failures`, and typed `failure_details`.

A business rejection is an authoritative domain outcome for the model to handle, not a transient infrastructure failure. Likewise, `pending_human_approval` is a valid successful tool result: it is not retried, is not counted as a reliability failure, and must not be described as completed.

```text
User task
  -> AgentRuntime
  -> reliability policy
  -> optional selected Skill
  -> MCP-discovered tool
  -> retry / timeout / repetition guard
  -> MCP server
  -> service layer
  -> SQLite
  -> structured result
  -> reliability accounting
  -> AgentRuntime
  -> final response or typed terminal outcome
```

The separation remains precise:

- **Reliability control:** runtime execution guard and recovery policy.
- **Skill:** reusable workflow guidance, progressively loaded only when selected.
- **Tool:** executable capability.
- **MCP:** protocol and capability-discovery boundary.
- **Service layer:** authoritative business rules.
- **Tracing:** a later phase and not implemented here.

Run the deterministic reliability scenarios and the normal Skill + MCP demonstration:

```bash
pytest tests/test_reliability.py
python -m app.skills.demo
```

The reliability tests inject scripted failures and a sleeper, so retry and timeout coverage uses no network and performs no real waiting.

## Phase 6 OpenTelemetry tracing

Tracing is a separate infrastructure layer around Agent, reliability, MCP, and Skill operations. It does not change business decisions or duplicate service logic. A tracer can be injected into `AgentRuntime`; when none is supplied, the OpenTelemetry API uses the process-configured provider or a no-op provider.

One run produces this hierarchy:

```text
agent.run
  -> skill.discovery
  -> mcp.discovery                       # when discovery occurs during preparation
  -> agent.step                          # one per logical Agent step
       -> agent.provider.call            # one per provider attempt
       -> reliability.retry              # one per actual retry/backoff
       -> agent.tool.call                # one per actual tool attempt
            -> skill.load                # for load_skill
            -> mcp.call                  # for MCP tools/call
```

Normal OpenTelemetry span timing supplies duration. Structured attributes cover the generated run ID, step, provider class and configured model name, safe tool/Skill names, MCP operation and protocol version, attempt/retry number, success/failure classification, refund completion status, terminal reason, and final counters. Root `agent.terminated` and sanitized `reliability.failure` events make completion and retry decisions reconstructable.

Trace data deliberately excludes prompts, assistant responses, tool arguments and results, customer/order identifiers, email addresses, idempotency keys, API keys, credentials, endpoint authorization, and exception messages. Failures are represented by typed categories and retryability only. `pending_human_approval` is recorded as a successful business result with `business.refund.completed=false`, not as infrastructure failure.

`create_in_memory_tracing()` creates an isolated `TracerProvider`, `SimpleSpanProcessor`, and `InMemorySpanExporter` without changing global OpenTelemetry state. It is used by tests and the deterministic local demo; no collector or network is required.

```text
User task
  -> agent.run
  -> reliability-controlled Agent step
  -> optional progressively loaded Skill
  -> MCP-discovered tool
  -> MCP server
  -> authoritative service layer
  -> SQLite
  -> structured result and sanitized trace outcome
  -> final response or typed terminal reason
```

Run the deterministic tracing demo and focused tests:

```bash
after-sales-tracing-demo
pytest tests/test_tracing.py
```

The demo prints only the sanitized in-memory span hierarchy and reliability counters. Exporter/collector deployment is intentionally not configured in this phase.

## Phase 7 automated Agent evaluations

Phase 7 evaluates observable end-to-end behavior rather than calling business services directly. Every case runs through `AgentRuntime`, the Skill metadata/loading boundary, MCP discovery and calls, Phase 5 reliability controls, Phase 6 tracing, the authoritative service layer, and an isolated temporary SQLite database.

The implementation is split into:

- `evals/cases.yaml`: versioned deterministic tasks, scripted provider turns, setup/fault declarations, and expected observable outcomes.
- `app/evals/models.py`: typed case, result, metric, aggregate, and regression-gate contracts.
- `app/evals/loader.py`: YAML loading, schema validation, duplicate detection, and case selection.
- `app/evals/runner.py`: provider-neutral execution, state observation, grading, aggregation, and threshold enforcement.
- `app/evals/report.py`: concise human-readable reporting; Pydantic results provide machine-readable JSON.
- `app/evals/cli.py`: full-suite, single-case, and selected-case command-line execution.

The case format is explicitly versioned. Expectations describe public outcomes, persisted state, calls, counters, and traces—not private implementation details:

```yaml
schema_version: "1.0"
suite_id: after-sales-agent-regression
thresholds:
  minimum_case_pass_rate: 1.0
  minimum_metric_pass_rate: 1.0
  maximum_failed_cases: 0
cases:
  - schema_version: "1.0"
    id: eligible-refund
    task: Inspect ORD-1024 and refund it if the service says it is eligible.
    script: [...]      # deterministic provider responses or typed provider errors
    expected: {...}    # tool behavior, state, counters, traces, and final outcome
```

The dataset covers delayed-order resolution, an eligible refund, high-value human approval, transient-provider recovery, retry exhaustion, repeated-call protection, unsafe support-ticket replay prevention, unknown tools, invalid arguments, and normal ticket creation. Refund thresholds and eligibility rules remain exclusively in `app/services.py`; the evaluator compares observable tool results and persisted state.

Per-case and aggregate metrics cover task/final-outcome correctness, required and forbidden/unnecessary tools, exact sequences and structured arguments, business/refund/approval state, side-effect safety, reliability and termination behavior, unsupported-claim risk, Skill and trace coverage, and step/model/tool/retry/failure counts. The configured regression gate fails when the case pass rate, any metric rate, or maximum failed-case threshold degrades.

Run all cases, one case, or a selected set:

```bash
after-sales-eval
after-sales-eval --case high-value-human-approval
after-sales-eval --case provider-transient-recovery --case provider-retry-exhaustion
after-sales-eval --format json
pytest tests/test_evals.py
```

The text report is intended for engineers; `--format json` emits the complete machine-readable result. A failed regression gate exits nonzero. All built-in cases use `ScriptedProvider`, in-process MCP, injected sleeping, in-memory tracing, and temporary databases, so no API key, external network, collector, or real wait is required.

## Phase 8 external benchmark integration

Phase 7 is this repository's deterministic regression suite: its cases, scripted
provider behavior, service state, and gates are owned locally. Phase 8 adds a
separate interoperability boundary for external benchmark tasks, execution, and
results. It does not relabel local evals as external benchmark runs and never turns
a process exit code into a score.

The first adapter targets Sierra Research's maintained
[τ³-bench implementation](https://github.com/sierra-research/tau2-bench). The
repository and Python package retain the `tau2` name, while the current benchmark
is branded τ³-bench. The legacy
[tau-bench repository](https://github.com/sierra-research/tau-bench) is outdated.
The adapter contract was reviewed against upstream `tau2` v1.0.1 and its current
[task schema](https://github.com/sierra-research/tau2-bench/blob/main/src/tau2/data_model/tasks.py),
[result schema](https://github.com/sierra-research/tau2-bench/blob/main/src/tau2/data_model/simulation.py),
[evaluation semantics](https://github.com/sierra-research/tau2-bench/blob/main/docs/evaluation.md),
and [CLI](https://github.com/sierra-research/tau2-bench/blob/main/docs/cli-reference.md).

The implementation is intentionally small and provider-neutral:

- `app/benchmarks/models.py` defines benchmark identity/version, domain, policy
  metadata, tools, input, expected observable outcome, execution result, score,
  outcome, errors, and aggregate report contracts.
- `BenchmarkAdapter` is the provider-neutral task/result conversion interface.
- `Tau3BenchmarkAdapter` maps the current upstream Task,
  EnvironmentInfo/tool-signature, Results, SimulationRun, RewardInfo, and
  termination fields without importing upstream code. Policy content is represented
  by an identifier and SHA-256 digest, not copied into normalized records.
- `ExternalProcessBenchmarkProvider` checks a separately installed executable and
  executes an explicit argument vector without a shell. Execution and result import
  remain separate; provider failures are typed.
- `report_from_local_evals` projects Phase 7 output into the common report shape but
  keeps the identity `after-sales-local-evals`, making its provenance unambiguous.

Upstream v1.0.1 requires Python `>=3.12,<3.14`, while this project supports Python
`>=3.11`. For that reason `tau2` is not a project dependency. Run it in its own
compatible environment and import its JSON result here. This preserves this
package's Python range and avoids pulling a large benchmark stack into the
application.

Upstream scoring is preserved: the final task reward is the product of components
listed in `evaluation_criteria.reward_basis`, and success means reward is within
`1e-6` of `1.0`. A task's `actions` are one reference trajectory used to derive a
target database state; they are a hard call-sequence requirement only when `ACTION`
is in the reward basis. The adapter records that distinction explicitly.

```text
Local deterministic evals                 External τ³ environment (Python 3.12)
  -> AgentRuntime/Skill/MCP                  -> registered benchmark Agent
  -> local regression report                -> tau2 run / upstream evaluator
                                                -> official Results JSON
                                                     -> Tau3BenchmarkAdapter
                                                     -> normalized report
```

Inspect availability and the supported contract/runtime boundary:

```bash
after-sales-benchmark inspect
after-sales-benchmark inspect --format json
```

Validate a task contract, validate imported results, or summarize results:

```bash
after-sales-benchmark validate-task path/to/task-contract.json
after-sales-benchmark validate-results path/to/results-contract.json
after-sales-benchmark summarize path/to/results-contract.json
after-sales-benchmark summarize path/to/upstream-results.json --benchmark-version 1.0.1
after-sales-benchmark summarize path/to/upstream-results.json --benchmark-version 1.0.1 --format json
```

The repository-owned `after-sales-benchmark/tau3-v1` task contract combines one
upstream-shaped Task with its domain name, policy, and discovered tool definitions,
because those pieces live in different upstream layers. A result contract adds
explicit benchmark-version provenance around an upstream-shaped Results object.
Raw Results are also accepted when `--benchmark-version` is supplied explicitly.

To prepare—but not execute—an upstream command for an Agent implementation that
has already been registered in a separately installed τ³ environment:

```bash
after-sales-benchmark command \
  --domain retail \
  --agent registered_agent \
  --agent-llm provider/model \
  --user-llm provider/model \
  --save-to enterprise-agent-run
```

Follow the official upstream installation instructions in that separate Python
3.12 environment, run the emitted `tau2 run` command there, and import the produced
Results JSON here. The command subcommand reports `executed: false`; this repository
does not claim a real run unless a real upstream result is supplied. It also does
not claim that the existing AgentRuntime is registered in τ³ automatically—an
upstream-compatible Agent factory must exist in the external environment.

Focused tests use only small synthetic repository-owned contracts:

```bash
pytest tests/test_benchmarks.py
```

They require no dataset download, API key, external network, real LLM, Docker, or
external benchmark installation. The fixtures are not official tasks or results
and must never be presented as benchmark scores. See `THIRD_PARTY_NOTICES.md` for
the upstream attribution and license boundary.

## Optional real model

Tests use `ScriptedProvider` and never need credentials or network access. To run against an OpenAI-compatible Chat Completions endpoint, configure values from `.env.example` in your shell:

```bash
export LLM_BASE_URL=https://api.openai.com/v1
export LLM_API_KEY=your-runtime-key
export LLM_MODEL=your-tool-calling-model
after-sales-agent "Handle delayed order ORD-1024 and report the outcome."
```

On PowerShell, use `$env:NAME="value"` instead of `export`. The CLI creates and seeds the configured SQLite database before running. No real-model execution result is claimed by this repository.

## Test

```bash
pytest
```

Tests use isolated temporary databases, the official SDK's in-process MCP path, an in-memory OpenTelemetry exporter, mocked HTTP where needed, and the deterministic scripted provider. The complete suite runs without external network access, an API key, a telemetry collector, or a paid model.
