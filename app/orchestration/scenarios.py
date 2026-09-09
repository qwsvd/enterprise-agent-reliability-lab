from __future__ import annotations

from app.orchestration.models import PlanTask, PlannedToolCall, TaskPlan


DELAYED_ORDER_GOAL = (
    "The customer says order ORD-1024 has still not arrived after 10 days. "
    "Resolve the issue according to the appropriate operational workflow."
)


def build_delayed_order_plan(plan_id: str = "delayed-order-plan") -> TaskPlan:
    """A deterministic demo plan; values are resolved from prior tool evidence."""
    return TaskPlan(
        plan_id=plan_id,
        goal=DELAYED_ORDER_GOAL,
        tasks=[
            PlanTask(
                task_id="load_workflow",
                objective="Load the delayed-order operational workflow.",
                required_tools=["load_skill"],
                tool_call=PlannedToolCall(
                    name="load_skill", arguments={"name": "delayed-order-resolution"}
                ),
                priority=10,
            ),
            PlanTask(
                task_id="inspect_order",
                objective="Inspect the current order and customer relationship.",
                dependencies=["load_workflow"],
                required_tools=["get_order"],
                tool_call=PlannedToolCall(
                    name="get_order", arguments={"order_code": "ORD-1024"}
                ),
                priority=20,
            ),
            PlanTask(
                task_id="inspect_customer",
                objective="Inspect the customer linked by order evidence.",
                dependencies=["inspect_order"],
                required_tools=["get_customer"],
                tool_call=PlannedToolCall(
                    name="get_customer",
                    arguments={"customer_code": {"$ref": "inspect_order.customer_code"}},
                ),
                priority=30,
            ),
            PlanTask(
                task_id="inspect_shipping",
                objective="Inspect shipment delay evidence.",
                dependencies=["inspect_order"],
                required_tools=["get_shipping"],
                tool_call=PlannedToolCall(
                    name="get_shipping", arguments={"order_code": "ORD-1024"}
                ),
                priority=40,
            ),
            PlanTask(
                task_id="inspect_policy",
                objective="Inspect the authoritative refund policy.",
                dependencies=["load_workflow"],
                required_tools=["get_refund_policy"],
                tool_call=PlannedToolCall(name="get_refund_policy", arguments={}),
                priority=50,
            ),
            PlanTask(
                task_id="create_refund",
                objective="Request the evidence-supported refund through the business tool.",
                dependencies=["inspect_order", "inspect_shipping", "inspect_policy"],
                required_tools=["create_refund"],
                tool_call=PlannedToolCall(
                    name="create_refund",
                    arguments={
                        "order_code": "ORD-1024",
                        "amount": {"$ref": "inspect_order.amount"},
                        "reason": "Shipment remains materially delayed",
                        "idempotency_key": f"{plan_id}-refund",
                    },
                ),
                priority=60,
            ),
            PlanTask(
                task_id="create_ticket",
                objective="Create a support ticket after the refund outcome is known.",
                dependencies=["inspect_customer", "create_refund"],
                required_tools=["create_support_ticket"],
                tool_call=PlannedToolCall(
                    name="create_support_ticket",
                    arguments={
                        "customer_code": {"$ref": "inspect_order.customer_code"},
                        "order_code": "ORD-1024",
                        "subject": "Delayed shipment resolution",
                        "description": "Shipment delay reviewed and refund outcome recorded",
                        "priority": "high",
                    },
                ),
                priority=70,
            ),
            PlanTask(
                task_id="verify_result",
                objective="Verify the persisted order refund state after side effects.",
                dependencies=["create_refund", "create_ticket"],
                required_tools=["get_order"],
                tool_call=PlannedToolCall(
                    name="get_order", arguments={"order_code": "ORD-1024"}
                ),
                priority=80,
            ),
        ],
    )
