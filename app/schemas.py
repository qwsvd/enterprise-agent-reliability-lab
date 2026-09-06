from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class HealthResponse(BaseModel):
    status: Literal["ok"]


class CustomerResponse(ApiModel):
    code: str
    name: str
    email: str
    created_at: datetime


class OrderResponse(ApiModel):
    code: str
    customer_code: str
    amount: Decimal
    currency: str
    status: str
    refunded: bool
    created_at: datetime


class ShipmentResponse(ApiModel):
    order_code: str
    tracking_code: str
    carrier: str
    status: str
    delayed_days: int
    shipped_at: datetime


class RefundPolicyResponse(BaseModel):
    minimum_delay_days: int
    human_approval_threshold: Decimal
    currency: Literal["CNY"]
    rules: list[str]


class RefundCreate(BaseModel):
    order_code: str = Field(min_length=1, max_length=32)
    amount: Decimal = Field(gt=0, decimal_places=2, max_digits=12)
    reason: str = Field(min_length=3, max_length=1000)


class RefundResponse(ApiModel):
    code: str
    order_code: str
    amount: Decimal
    currency: str
    reason: str
    status: Literal["approved", "pending_human_approval"]
    created_at: datetime


class TicketCreate(BaseModel):
    customer_code: str = Field(min_length=1, max_length=32)
    order_code: str | None = Field(default=None, max_length=32)
    subject: str = Field(min_length=3, max_length=160)
    description: str = Field(min_length=3, max_length=4000)
    priority: Literal["low", "normal", "high", "urgent"] = "normal"


class TicketResponse(ApiModel):
    code: str
    customer_code: str
    order_code: str | None
    subject: str
    description: str
    priority: str
    status: Literal["open"]
    created_at: datetime

