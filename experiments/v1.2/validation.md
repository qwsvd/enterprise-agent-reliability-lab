# Validation record

The starting commit 9aa26074ac40360e2051cf033aab3cb90395c4f5 declared version 1.1.0. Its full suite passed 135 tests on Windows. The existing AgentRuntime alternates policy and tool steps. The orchestration graph already routes through planner, scheduler, executor and reviewer, maintains working and episodic memory, and supports reflection and replanning. ProviderPlanner accepts provider output, whereas the previous demonstrations and regression fixtures supply known actions or plans. Those runs provide no measured hosted-model comparison.

| Existing implementation | Reused in this study |
|---|---|
| app/agent/runtime.py and app/reliability/control.py | Online decision loop, counters and bounded retries |
| app/orchestration/{graph,planner,scheduler,executor,reviewer}.py | Generate and validate a dependency plan, select ready tasks, execute, review evidence, optionally replan |
| app/orchestration/memory.py | Store sanitized episodes; copy the same development seed to each run |
| app/mcp_integration and app/skills/tools.py | Discover business tools and expose skill guidance through the same adapter |
| app/tracing/core.py | In-memory OpenTelemetry traces, separate from experiment decision records |
| app/evals/runner.py and app/orchestration/evals.py | Existing deterministic regression suites retained |
| app/benchmarks/tau3.py | Existing external benchmark adapter retained; this experiment uses local SQLite orders |

Stockroom Loop demonstrated the importance of checking business state separately from a model's statement and explicitly supplying missing authorization. Here each case supplies scope directly. The enterprise pilot does not measure multi-turn clarification. [Stockroom clarification experiment](https://github.com/qwsvd/stockroom-loop/blob/v0.3.0/docs/clarification-v0.3.md)

Windows, Python 3.14.3: final `python -m pytest -q` passed 151 tests in 14.04 seconds. Tests include malformed decisions, attempts to populate executor evidence, unsupported approval statements, quota signals, unexpected native tool events and a second ticket with changed arguments. After the live pilot a defense-in-depth check was added to stop on unexpected native tool events; that path was tested with a fake CLI. The native pilot was not repeated.

`python -m app.evals.workflow replay experiments/v1.2/live.jsonl` rebuilt six fresh databases and matched six archived outcomes including the two invalid plans. Replay checks the business snapshot, dispatched names/arguments and success flags, decisions released, counters and review outcomes. Generated identifiers, timestamps and wall time are outside byte-level replay. Each JSONL line is one complete synthetic trial. Write failures or process loss before a line is flushed can leave an incomplete archive. The report checks the expected schedule, resource ceilings and shared initial database hashes.
