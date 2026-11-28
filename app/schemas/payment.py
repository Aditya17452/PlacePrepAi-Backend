"""Plans, orders and payment status."""

from __future__ import annotations

from pydantic import Field

from app.schemas.common import OutModel, StrictModel


class PlanOut(OutModel):
    code: str
    name: str
    price_paise: int
    tokens: int
    validity_days: int | None
    is_recommended: bool = False
    requires_phone_verification: bool = False


class CreateOrderRequest(StrictModel):
    """Only a plan code. The server looks up price and token count."""

    plan_code: str = Field(min_length=1, max_length=40)


class CreateOrderResponse(OutModel):
    order_id: str
    amount_paise: int
    currency: str
    razorpay_key_id: str
    plan: PlanOut


class PaymentStatusResponse(OutModel):
    """What the client-side callback gets: status only.

    Tokens are granted by the webhook, so this endpoint cannot be replayed for credit.
    """

    order_id: str
    status: str
    tokens_granted: int
    message: str


class RefundRequest(StrictModel):
    payment_id: str = Field(max_length=64)
    reason: str = Field(min_length=3, max_length=500)
    #: Omitted means a full refund.
    amount_paise: int | None = Field(default=None, gt=0)
