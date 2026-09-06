from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.schemas import (
    CustomerResponse, HealthResponse, OrderResponse, RefundCreate,
    RefundPolicyResponse, RefundResponse, ShipmentResponse, TicketCreate, TicketResponse,
)
from app.services import (
    HUMAN_APPROVAL_THRESHOLD, MINIMUM_DELAY_DAYS, ServiceError,
    create_refund, create_ticket, get_customer, get_order,
)

router = APIRouter()


def get_session() -> Session:
    raise RuntimeError("Database dependency was not configured")


def _error(error: ServiceError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.detail)


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/customers/{customer_code}", response_model=CustomerResponse)
def customer(customer_code: str, session: Session = Depends(get_session)) -> CustomerResponse:
    try:
        return CustomerResponse.model_validate(get_customer(session, customer_code))
    except ServiceError as exc:
        raise _error(exc) from exc


@router.get("/orders/{order_code}", response_model=OrderResponse)
def order(order_code: str, session: Session = Depends(get_session)) -> OrderResponse:
    try:
        item = get_order(session, order_code)
    except ServiceError as exc:
        raise _error(exc) from exc
    return OrderResponse(
        code=item.code, customer_code=item.customer.code, amount=item.amount,
        currency=item.currency, status=item.status, refunded=item.refund is not None,
        created_at=item.created_at,
    )


@router.get("/orders/{order_code}/shipping", response_model=ShipmentResponse)
def shipping(order_code: str, session: Session = Depends(get_session)) -> ShipmentResponse:
    try:
        item = get_order(session, order_code)
    except ServiceError as exc:
        raise _error(exc) from exc
    if item.shipment is None:
        raise HTTPException(404, f"Shipment for order {order_code} not found")
    return ShipmentResponse(
        order_code=item.code, tracking_code=item.shipment.tracking_code,
        carrier=item.shipment.carrier, status=item.shipment.status,
        delayed_days=item.shipment.delayed_days, shipped_at=item.shipment.shipped_at,
    )


@router.get("/policies/refund", response_model=RefundPolicyResponse)
def policy() -> RefundPolicyResponse:
    return RefundPolicyResponse(
        minimum_delay_days=MINIMUM_DELAY_DAYS,
        human_approval_threshold=HUMAN_APPROVAL_THRESHOLD,
        currency="CNY",
        rules=[
            "A shipment delayed by at least 7 days may qualify.",
            "An already-refunded order cannot be refunded again.",
            "The refund amount cannot exceed the order amount.",
            "Refunds above 1000 CNY require human approval.",
            "Refund creation requires an idempotency key.",
        ],
    )


@router.post("/refunds", response_model=RefundResponse, status_code=201)
def refund(
    request: RefundCreate,
    response: Response,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    session: Session = Depends(get_session),
) -> RefundResponse:
    try:
        item, created = create_refund(session, request, idempotency_key)
    except ServiceError as exc:
        raise _error(exc) from exc
    if not created:
        response.status_code = status.HTTP_200_OK
    return RefundResponse(
        code=item.code, order_code=item.order.code, amount=item.amount,
        currency=item.currency, reason=item.reason, status=item.status,
        created_at=item.created_at,
    )


@router.post("/tickets", response_model=TicketResponse, status_code=201)
def ticket(request: TicketCreate, session: Session = Depends(get_session)) -> TicketResponse:
    try:
        item = create_ticket(session, request)
    except ServiceError as exc:
        raise _error(exc) from exc
    return TicketResponse(
        code=item.code, customer_code=item.customer.code,
        order_code=item.order.code if item.order else None, subject=item.subject,
        description=item.description, priority=item.priority, status=item.status,
        created_at=item.created_at,
    )

