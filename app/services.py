import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.models import Customer, Order, Refund, SupportTicket
from app.schemas import RefundCreate, TicketCreate

MINIMUM_DELAY_DAYS = 7
HUMAN_APPROVAL_THRESHOLD = Decimal("1000.00")


@dataclass(frozen=True)
class ServiceError(Exception):
    status_code: int
    detail: str


def get_customer(session: Session, code: str) -> Customer:
    customer = session.scalar(select(Customer).where(Customer.code == code))
    if customer is None:
        raise ServiceError(404, f"Customer {code} not found")
    return customer


def get_order(session: Session, code: str) -> Order:
    order = session.scalar(
        select(Order).options(
            joinedload(Order.customer), joinedload(Order.shipment), joinedload(Order.refund)
        ).where(Order.code == code)
    )
    if order is None:
        raise ServiceError(404, f"Order {code} not found")
    return order


def _fingerprint(request: RefundCreate) -> str:
    canonical = json.dumps({
        "order_code": request.order_code,
        "amount": format(request.amount, ".2f"),
        "reason": request.reason,
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def create_refund(
    session: Session, request: RefundCreate, idempotency_key: str
) -> tuple[Refund, bool]:
    fingerprint = _fingerprint(request)
    existing = session.scalar(
        select(Refund).options(joinedload(Refund.order)).where(
            Refund.idempotency_key == idempotency_key
        )
    )
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise ServiceError(409, "Idempotency key was already used for a different request")
        return existing, False

    order = get_order(session, request.order_code)
    if order.refund is not None:
        raise ServiceError(409, "Order has already been refunded")
    if request.amount > order.amount:
        raise ServiceError(422, "Refund amount cannot exceed order amount")
    if order.shipment is None or order.shipment.delayed_days < MINIMUM_DELAY_DAYS:
        raise ServiceError(422, "Order is not eligible: shipment delay must be at least 7 days")

    refund = Refund(
        code=f"REF-{uuid4().hex[:10].upper()}", order=order, amount=request.amount,
        currency=order.currency, reason=request.reason,
        status=("pending_human_approval" if request.amount > HUMAN_APPROVAL_THRESHOLD else "approved"),
        idempotency_key=idempotency_key, request_fingerprint=fingerprint,
    )
    session.add(refund)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        concurrent = session.scalar(
            select(Refund).options(joinedload(Refund.order)).where(
                Refund.idempotency_key == idempotency_key
            )
        )
        if concurrent is not None and concurrent.request_fingerprint == fingerprint:
            return concurrent, False
        raise ServiceError(409, "Refund conflicts with an existing refund") from exc
    session.refresh(refund)
    return refund, True


def create_ticket(session: Session, request: TicketCreate) -> SupportTicket:
    customer = get_customer(session, request.customer_code)
    order = get_order(session, request.order_code) if request.order_code else None
    if order is not None and order.customer_id != customer.id:
        raise ServiceError(422, "Order does not belong to the specified customer")
    ticket = SupportTicket(
        code=f"TKT-{uuid4().hex[:10].upper()}", customer=customer, order=order,
        subject=request.subject, description=request.description,
        priority=request.priority, status="open",
    )
    session.add(ticket)
    session.commit()
    session.refresh(ticket)
    return ticket

