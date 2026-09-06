---
name: high-value-refund-escalation
description: Handle a refund request that may require human approval while preserving authoritative policy and communicating escalation state accurately.
---

# High-value refund escalation

1. Use `get_order` to inspect the order amount, currency, and existing refund state.
2. Use `get_refund_policy` and treat the returned values as authoritative. Do not rely on a copied threshold or make approval decisions in this workflow.
3. When a refund attempt is appropriate, call `create_refund` with a stable idempotency key. Never update refund state directly or try to bypass human approval.
4. Interpret the returned state precisely:
   - `approved` with `completed: true` means the refund completed.
   - `pending_human_approval` means the request was recorded but is not completed.
   - A rejected or failed result means no successful refund should be claimed.
5. Communicate whether the refund was requested, pending, approved, rejected, or failed, and identify any required human follow-up without inventing an approval workflow.

