from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import Order, Refund, Shipment


def refund_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "order_code": "ORD-1024", "amount": "299.00",
        "reason": "Shipment delayed by 10 days",
    }
    payload.update(overrides)
    return payload


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_customer_lookup(client: TestClient) -> None:
    response = client.get("/customers/CUS-001")
    assert response.status_code == 200
    assert response.json()["name"] == "Li Wei"


def test_order_lookup(client: TestClient) -> None:
    response = client.get("/orders/ORD-1024")
    assert response.status_code == 200
    body = response.json()
    assert body["customer_code"] == "CUS-001"
    assert body["amount"] == "299.00"
    assert body["currency"] == "CNY"
    assert body["refunded"] is False


def test_shipping_lookup(client: TestClient) -> None:
    response = client.get("/orders/ORD-1024/shipping")
    assert response.status_code == 200
    assert response.json()["status"] == "delayed"
    assert response.json()["delayed_days"] == 10


def test_refund_policy(client: TestClient) -> None:
    response = client.get("/policies/refund")
    assert response.status_code == 200
    assert response.json()["minimum_delay_days"] == 7
    assert response.json()["human_approval_threshold"] == "1000.00"


def test_successful_eligible_refund(client: TestClient) -> None:
    response = client.post(
        "/refunds", json=refund_payload(), headers={"Idempotency-Key": "refund-1"}
    )
    assert response.status_code == 201
    assert response.json()["status"] == "approved"
    assert response.json()["amount"] == "299.00"
    assert client.get("/orders/ORD-1024").json()["refunded"] is True


def test_duplicate_refund_prevention(client: TestClient) -> None:
    first = client.post(
        "/refunds", json=refund_payload(), headers={"Idempotency-Key": "refund-1"}
    )
    second = client.post(
        "/refunds", json=refund_payload(amount="100.00"),
        headers={"Idempotency-Key": "refund-2"},
    )
    assert first.status_code == 201
    assert second.status_code == 409
    assert "already been refunded" in second.json()["detail"]


def test_idempotent_retry_returns_same_refund(client: TestClient) -> None:
    headers = {"Idempotency-Key": "stable-key"}
    first = client.post("/refunds", json=refund_payload(), headers=headers)
    second = client.post("/refunds", json=refund_payload(), headers=headers)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json() == first.json()
    with client.app.state.database.session_factory() as session:
        assert len(session.scalars(select(Refund)).all()) == 1


def test_changed_idempotent_request_is_rejected(client: TestClient) -> None:
    headers = {"Idempotency-Key": "stable-key"}
    client.post("/refunds", json=refund_payload(), headers=headers)
    response = client.post(
        "/refunds", json=refund_payload(amount="100.00"), headers=headers
    )
    assert response.status_code == 409


def test_invalid_order(client: TestClient) -> None:
    response = client.post(
        "/refunds", json=refund_payload(order_code="ORD-MISSING"),
        headers={"Idempotency-Key": "missing-order"},
    )
    assert response.status_code == 404


def test_excessive_refund_amount(client: TestClient) -> None:
    response = client.post(
        "/refunds", json=refund_payload(amount="300.00"),
        headers={"Idempotency-Key": "too-much"},
    )
    assert response.status_code == 422
    assert "cannot exceed" in response.json()["detail"]


def test_policy_ineligible_refund(client: TestClient) -> None:
    with client.app.state.database.session_factory() as session:
        order = session.scalar(select(Order).where(Order.code == "ORD-1024"))
        shipment = session.scalar(select(Shipment).where(Shipment.order_id == order.id))
        shipment.delayed_days = 6
        session.commit()
    response = client.post(
        "/refunds", json=refund_payload(), headers={"Idempotency-Key": "ineligible"}
    )
    assert response.status_code == 422
    assert "at least 7 days" in response.json()["detail"]


def test_support_ticket_creation(client: TestClient) -> None:
    response = client.post("/tickets", json={
        "customer_code": "CUS-001", "order_code": "ORD-1024",
        "subject": "Delayed shipment",
        "description": "Please provide an updated delivery date.", "priority": "high",
    })
    assert response.status_code == 201
    assert response.json()["customer_code"] == "CUS-001"
    assert response.json()["order_code"] == "ORD-1024"
    assert response.json()["status"] == "open"


def test_refund_above_threshold_requires_human_approval(client: TestClient) -> None:
    with client.app.state.database.session_factory() as session:
        order = session.scalar(select(Order).where(Order.code == "ORD-1024"))
        order.amount = Decimal("1500.00")
        session.commit()
    response = client.post(
        "/refunds", json=refund_payload(amount="1200.00"),
        headers={"Idempotency-Key": "human-approval"},
    )
    assert response.status_code == 201
    assert response.json()["status"] == "pending_human_approval"


def test_refund_requires_idempotency_key(client: TestClient) -> None:
    assert client.post("/refunds", json=refund_payload()).status_code == 422

