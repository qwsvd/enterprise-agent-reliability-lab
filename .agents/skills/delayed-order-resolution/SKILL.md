---
name: delayed-order-resolution
description: Resolve a materially delayed customer order by inspecting business state, applying the current refund policy through tools, and documenting the outcome.
---

# Delayed order resolution

1. Identify the order and use `get_order` to obtain its customer, amount, currency, and current refund state.
2. Use `get_customer` for the identified customer and `get_shipping` for the order. Do not assume delay state from the user's wording alone.
3. Use `get_refund_policy` and treat its returned policy as authoritative. This workflow does not replace business-policy enforcement.
4. If the observed state makes a refund attempt appropriate, call `create_refund` with an amount no greater than the observed order amount, a clear reason, and a stable idempotency key. Let the business tool decide eligibility and approval state.
5. Create a support ticket when follow-up or documentation is appropriate. Include the observed delay and actual refund-tool outcome.
6. Report the exact result:
   - Say a refund completed only when the tool returns `approved` and `completed: true`.
   - Describe `pending_human_approval` as pending, never completed.
   - Describe business rejection or tool failure accurately without claiming a refund occurred.

Never mutate the database directly or bypass the business tools.

