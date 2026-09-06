from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from app.schemas import RefundCreate, TicketCreate
from app.services import (
    ServiceError,
    create_refund,
    create_ticket,
    get_customer,
    get_order,
    get_refund_policy,
)


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CustomerInput(ToolInput):
    customer_code: str = Field(min_length=1, max_length=32)


class OrderInput(ToolInput):
    order_code: str = Field(min_length=1, max_length=32)


class RefundPolicyInput(ToolInput):
    pass


class CreateRefundInput(ToolInput):
    order_code: str = Field(min_length=1, max_length=32)
    amount: Decimal = Field(gt=0, decimal_places=2, max_digits=12)
    reason: str = Field(min_length=3, max_length=1000)
    idempotency_key: str = Field(min_length=1, max_length=128)


class CreateSupportTicketInput(ToolInput):
    customer_code: str = Field(min_length=1, max_length=32)
    order_code: str | None = Field(default=None, max_length=32)
    subject: str = Field(min_length=3, max_length=160)
    description: str = Field(min_length=3, max_length=4000)
    priority: Literal["low", "normal", "high", "urgent"] = "normal"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[ToolInput]
    handler: Callable[[ToolInput], dict[str, Any]]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_model.model_json_schema(),
            },
        }


class ToolRegistry:
    def __init__(self, session: Session) -> None:
        self.session = session
        specs = [
            ToolSpec("get_customer", "Look up a customer by code.", CustomerInput, self._customer),
            ToolSpec("get_order", "Look up an order and its refund state.", OrderInput, self._order),
            ToolSpec("get_shipping", "Look up shipping state for an order.", OrderInput, self._shipping),
            ToolSpec(
                "get_refund_policy", "Read the current refund business policy.",
                RefundPolicyInput, self._refund_policy,
            ),
            ToolSpec("create_refund", "Create an eligible refund request.", CreateRefundInput, self._refund),
            ToolSpec(
                "create_support_ticket", "Create a customer support ticket.",
                CreateSupportTicketInput, self._ticket,
            ),
        ]
        self._specs = {spec.name: spec for spec in specs}

    def schemas(self) -> list[dict[str, Any]]:
        return [spec.schema() for spec in self._specs.values()]

    def execute(
        self, name: str, arguments: Any, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        spec = self._specs.get(name)
        if spec is None:
            return self._failure("unknown_tool", f"Unknown tool: {name}")
        try:
            validated = spec.input_model.model_validate(arguments)
        except ValidationError as exc:
            return self._failure(
                "invalid_arguments",
                "Tool arguments failed validation",
                exc.errors(include_context=False, include_url=False),
            )
        try:
            return {"ok": True, "data": spec.handler(validated)}
        except ServiceError as exc:
            return self._failure("business_error", exc.detail, {"status_code": exc.status_code})
        except Exception as exc:  # keep tool failures inside the model loop
            return self._failure("tool_execution_error", str(exc))

    @staticmethod
    def _failure(
        kind: str,
        message: str,
        details: Any = None,
        *,
        retryable: bool = False,
        ambiguous: bool = False,
    ) -> dict[str, Any]:
        error: dict[str, Any] = {"type": kind, "message": message}
        if details is not None:
            error["details"] = details
        if retryable:
            error["retryable"] = True
        if ambiguous:
            error["ambiguous"] = True
        return {"ok": False, "error": error}

    def _customer(self, request: ToolInput) -> dict[str, Any]:
        assert isinstance(request, CustomerInput)
        item = get_customer(self.session, request.customer_code)
        return {
            "code": item.code, "name": item.name, "email": item.email,
            "created_at": item.created_at.isoformat(),
        }

    def _order(self, request: ToolInput) -> dict[str, Any]:
        assert isinstance(request, OrderInput)
        item = get_order(self.session, request.order_code)
        refund_status = item.refund.status if item.refund else None
        return {
            "code": item.code, "customer_code": item.customer.code,
            "amount": str(item.amount), "currency": item.currency, "status": item.status,
            "refunded": refund_status == "approved", "refund_status": refund_status,
        }

    def _shipping(self, request: ToolInput) -> dict[str, Any]:
        assert isinstance(request, OrderInput)
        item = get_order(self.session, request.order_code)
        if item.shipment is None:
            raise ServiceError(404, f"Shipment for order {request.order_code} not found")
        return {
            "order_code": item.code, "tracking_code": item.shipment.tracking_code,
            "carrier": item.shipment.carrier, "status": item.shipment.status,
            "delayed_days": item.shipment.delayed_days,
            "shipped_at": item.shipment.shipped_at.isoformat(),
        }

    def _refund_policy(self, request: ToolInput) -> dict[str, Any]:
        policy = get_refund_policy()
        return {**policy, "human_approval_threshold": str(policy["human_approval_threshold"])}

    def _refund(self, request: ToolInput) -> dict[str, Any]:
        assert isinstance(request, CreateRefundInput)
        item, created = create_refund(
            self.session,
            RefundCreate(order_code=request.order_code, amount=request.amount, reason=request.reason),
            request.idempotency_key,
        )
        return {
            "code": item.code, "order_code": item.order.code, "amount": str(item.amount),
            "currency": item.currency, "status": item.status, "created": created,
            "completed": item.status == "approved",
        }

    def _ticket(self, request: ToolInput) -> dict[str, Any]:
        assert isinstance(request, CreateSupportTicketInput)
        item = create_ticket(self.session, TicketCreate(**request.model_dump()))
        return {
            "code": item.code, "customer_code": item.customer.code,
            "order_code": item.order.code if item.order else None,
            "status": item.status, "priority": item.priority,
        }
