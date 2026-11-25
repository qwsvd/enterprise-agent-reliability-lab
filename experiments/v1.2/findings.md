# Matched workflow pilot

The high-value order produced one CNY 1,499 refund request pending human approval in every arm. No payment was recorded as completed. In the second order the first shipment read returned a retryable transport error. A retried the read and obtained an approved CNY 299 refund. Both graph arms used one reflection and requested a revised plan. Their replies carried executor-owned task state into the new plan, including a ready task with a previous attempt. ProviderPlanner rejected those replies. Both databases remained free of refunds and tickets. The test with a changing shipment delay is a separate offline case.

| Frozen task | A | B | C |
|---|---|---|---|
| Approval required | pending, 3 calls | pending, 1 call | pending, 1 call |
| Transient shipment failure | approved, 3 calls | invalid plan, 2 calls | invalid plan, 2 calls |

Every native invocation used ChatGPT login and codex-cli 0.171.0-alpha.13.2 with requested model gpt-6.2-sol. The 12 recorded invocations yielded 167,517 input tokens and 3,630 output tokens in total. Cached tokens are a subset of input. Backend identity, internal service retries and monetary cost remain unknown. The pilot used 12 of 24 allowed calls. Each invocation ran in a fresh temporary directory, with a read-only sandbox and CLI shell, apps, plugins and web search disabled. Only the Python service held the business connection. These controls apply to this harness and CLI configuration.

The graph saved model turns on the approval task. On the failed read it paid for a second plan without delivering the refund. Across the pair all three arms took about 53–54 seconds each. The C arm retrieved one development episode in each trial and still produced the invalid replan. The evidence supports keeping A as the reference implementation for this workflow. Two task instances, one draw per arm and one fixed development episode cannot estimate population success rates, isolate a memory effect or establish any advantage from multiple independent model agents. Architecture changes message shape and context length. Equal ceilings do not ensure equal token consumption or identical serving weights.

The scripted integration archive exercises sixteen cases in each arm. A met 16 expectations. B and C each met 14. In both graph arms the unknown order and mid-task state change ended without the specific required diagnosis. No unauthorized ledger changes were observed. The scripts have access to the expected outcome, so these counts measure runtime behavior under supplied decisions. The harness gives all arms an explicit authorization scope. The missing-information case checks the clarification disposition only. It does not test interpreting a follow-up answer. The single-order ticket guard assumes one sequential workflow; cross-process exactly-once delivery is outside this experiment.

The release gate checks canonical dispositions and queries actual refund and ticket rows. The simulator controls both the task and its database. Snapshot code shares ORM definitions with the service, and a plausible but irrelevant successful tool response can inflate the usable-tool proxy. We did not measure production incident rates, real customers, a human approval decision or performance on τ³-bench. Independent graders here means deterministic checks separate from the model's text.

**Next experiment.** Give the planner a proposal-only schema for revisions, separate from the execution-state snapshot it reads. Freeze new fault cases, compare it against this archived baseline, and count rejected revisions and verified recoveries. Then vary retrieval alone at a fixed replan budget. Preserve invalid proposals in both studies.

[Protocol](protocol.md) · [Frozen cases](tasks.json) · [Raw real decisions](live.jsonl) · [All aggregate measures and definitions](scorecard.md) · [Offline records](offline-final.jsonl) · [Earlier preflight](preflight.md)
