# Enterprise Agent Reliability Lab

Phase 1 is a typed local after-sales backend for future AI agents. It models customers, orders, shipments, refunds, and support tickets with FastAPI, Pydantic, SQLAlchemy, and SQLite.

## Setup and run

Requires Python 3.11+.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -e ".[dev]"
uvicorn app.main:app --reload
```

The default database is `sqlite:///./after_sales.db`. Override it with `DATABASE_URL` as shown in `.env.example`. API docs are at `http://127.0.0.1:8000/docs`.

## Demo data and API

Startup idempotently seeds customer `CUS-001`, order `ORD-1024` for `299.00 CNY`, and its shipment delayed by 10 days.

- `GET /health`
- `GET /customers/{customer_code}`
- `GET /orders/{order_code}`
- `GET /orders/{order_code}/shipping`
- `GET /policies/refund`
- `POST /refunds` (requires `Idempotency-Key` header)
- `POST /tickets`

Shipments delayed at least 7 days may qualify. Orders can only be refunded once; the refund cannot exceed the order amount; and refunds above 1000 CNY require human approval. An identical idempotent retry returns the original refund, while reusing a key for another request is rejected.

## Test

```bash
pytest
```

Tests use isolated temporary databases.
