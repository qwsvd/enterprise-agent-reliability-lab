from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Customer, Order, Shipment


def seed_demo_data(session: Session) -> None:
    if session.scalar(select(Customer).where(Customer.code == "CUS-001")) is not None:
        return
    customer = Customer(code="CUS-001", name="Li Wei", email="li.wei@example.com")
    order = Order(
        code="ORD-1024", customer=customer, amount=Decimal("299.00"),
        currency="CNY", status="shipped"
    )
    shipment = Shipment(
        order=order, tracking_code="SF-20260906001024", carrier="SF Express",
        status="delayed", delayed_days=10,
        shipped_at=datetime.now(UTC) - timedelta(days=14),
    )
    session.add_all([customer, order, shipment])
    session.commit()
