"""Payments (PLAN.md §8.5).

This module replaces `api/credits_payment.py`, which had three serious problems:

1. An unauthenticated `POST /api/payments/{student_id}/add` route appended at the bottom
   of the file granted anyone any number of credits. **It is gone.**
2. `/verify` granted credits and then wrote `payment_logs` with the SQLite-only
   `lower(hex(randomblob(16)))` into a table that does not exist in Supabase — the insert
   raised, `db.rollback()` ran, and the credit grant was rolled back with it. Successful
   payments granted nothing.
3. There was no idempotency, so one valid signature could be replayed for unlimited
   credits, and no webhook at all.

Now: prices come from the `plans` table (the client sends only a code), tokens are granted
**only** from the signature-verified `payment.captured` webhook, and `payments.payment_id`
is unique, which is what makes the grant idempotent against retries and replays.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError

from app.api.deps import Admin, CurrentUser, DbSession
from app.core import audit, ratelimit
from app.core.config import settings
from app.core.logging_setup import get_logger
from app.integrations import razorpay as rzp
from app.models import Payment, Plan, Student, utcnow
from app.schemas import (
    CreateOrderRequest,
    CreateOrderResponse,
    PaymentStatusResponse,
    PlanOut,
    RefundRequest,
    SimpleOk,
)
from app.services import token_service

log = get_logger("payments")

router = APIRouter(prefix="/api/payments", tags=["payments"])


# ── Plans ─────────────────────────────────────────────────────────────────────


@router.get("/plans", response_model=list[PlanOut])
async def list_plans(db: DbSession) -> list[PlanOut]:
    """Public: the direct-student plans that are currently on sale."""
    plans = (
        db.query(Plan)
        .filter(Plan.is_active.is_(True), Plan.audience == "student")
        .order_by(Plan.sort_order, Plan.price_paise)
        .all()
    )
    return [PlanOut.model_validate(p) for p in plans]


# ── Order creation ────────────────────────────────────────────────────────────


@router.post("/create-order", response_model=CreateOrderResponse)
async def create_order(
    body: CreateOrderRequest,
    request: Request,
    principal: CurrentUser,
    db: DbSession,
) -> CreateOrderResponse:
    """Create a Razorpay order for the authenticated student.

    The amount is read from the `plans` table, never from the request, so a tampered
    client cannot buy the Rs 999 bundle for Rs 1.
    """
    await ratelimit.limit_by_user(principal.id, "payments", "10/600")

    plan = (
        db.query(Plan)
        .filter(
            Plan.code == body.plan_code,
            Plan.is_active.is_(True),
            Plan.audience == "student",
        )
        .first()
    )
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That plan is not available.")
    if plan.price_paise == 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Free tokens are granted automatically once your email and phone are "
            "verified — there is nothing to pay for.",
        )

    try:
        order = rzp.create_order(
            amount_paise=int(plan.price_paise),
            notes={
                "student_id": principal.id,
                "plan_code": plan.code,
                "tokens": str(plan.tokens),
            },
        )
    except rzp.RazorpayNotConfigured as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Payments are not configured on this server.",
        ) from exc
    except rzp.RazorpayError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Could not start the payment. Please try again.",
        ) from exc

    db.add(
        Payment(
            student_id=principal.id,
            plan_code=plan.code,
            order_id=order["id"],
            amount_paise=int(plan.price_paise),
            status="created",
            # 18% GST is included in the listed price (PLAN.md §9).
            gst_paise=int(round(plan.price_paise - plan.price_paise / 1.18)),
        )
    )
    audit.record(
        db,
        action="payment.order_created",
        actor_id=principal.id,
        actor_role=principal.role.value,
        target=audit.target_for("order", order["id"]),
        detail={"plan": plan.code, "amount_paise": plan.price_paise},
        ip=ratelimit.client_ip(request),
    )
    db.commit()

    return CreateOrderResponse(
        order_id=order["id"],
        amount_paise=int(plan.price_paise),
        currency="INR",
        razorpay_key_id=settings.RAZORPAY_KEY_ID,
        plan=PlanOut.model_validate(plan),
    )


# ── Webhook: the only place tokens are granted ────────────────────────────────


@router.post("/webhook", include_in_schema=False)
async def razorpay_webhook(request: Request, db: DbSession) -> dict[str, str]:
    """Razorpay webhook. Verifies the signature over the raw body, then grants tokens.

    Returns 200 for anything already processed so Razorpay stops retrying, and 400 only
    for a genuinely bad signature.
    """
    if not settings.RAZORPAY_WEBHOOK_SECRET:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Webhook not configured"
        )

    # Verified over the RAW body: parsing and re-serialising would change the bytes.
    raw = await request.body()
    if not rzp.verify_webhook_signature(
        raw, request.headers.get("X-Razorpay-Signature", "")
    ):
        log.warning("webhook_bad_signature")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid signature")

    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Malformed payload") from None

    event_type = event.get("event", "")
    # Not `event=` — structlog reserves that key for the message itself.
    log.info("webhook_received", event_type=event_type)

    if event_type not in ("payment.captured", "payment.failed", "refund.processed"):
        return {"status": "ignored"}

    entity = rzp.extract_entity(event)
    order_id = entity.get("order_id")
    payment_id = entity.get("id")
    if not order_id:
        return {"status": "ignored"}

    payment = db.query(Payment).filter(Payment.order_id == order_id).first()
    if payment is None:
        # An order we never created. Record nothing, acknowledge so retries stop.
        log.warning("webhook_unknown_order", order_id=order_id)
        return {"status": "unknown_order"}

    if event_type == "payment.failed":
        payment.status = "failed"
        payment.raw_webhook = event
        db.commit()
        return {"status": "recorded"}

    if event_type == "refund.processed":
        _apply_refund_from_webhook(db, payment, entity, event)
        return {"status": "refunded"}

    # payment.captured — the grant path.
    if payment.status == "captured" and payment.tokens_granted > 0:
        return {"status": "already_processed"}

    plan = db.query(Plan).filter(Plan.code == payment.plan_code).first()
    if plan is None:  # pragma: no cover - plans are never deleted, only deactivated
        log.error("webhook_plan_missing", plan=payment.plan_code)
        return {"status": "plan_missing"}

    student = db.query(Student).filter(Student.id == payment.student_id).first()
    if student is None:
        log.error("webhook_student_missing", student_id=payment.student_id)
        return {"status": "student_missing"}

    payment.payment_id = payment_id
    payment.status = "captured"
    payment.signature_verified = True
    payment.raw_webhook = event
    payment.captured_at = utcnow()
    payment.tokens_granted = int(plan.tokens)

    token_service.grant(
        db,
        owner_type="student",
        owner_id=str(student.id),
        qty=int(plan.tokens),
        reason=f"Purchase of {plan.code} ({payment.order_id})",
        actor_id=None,
        validity_days=plan.validity_days,
        source="purchase",
        payment_id=payment_id,
        plan_code=plan.code,
    )
    audit.record(
        db,
        action="payment.captured",
        actor_id=str(student.id),
        actor_role="student",
        target=audit.target_for("payment", payment_id),
        detail={"plan": plan.code, "tokens": plan.tokens},
    )

    try:
        db.commit()
    except IntegrityError:
        # The unique constraint on payment_id rejected a concurrent duplicate webhook.
        # That is the idempotency guarantee doing its job.
        db.rollback()
        log.info("webhook_duplicate_payment_id", payment_id=payment_id)
        return {"status": "already_processed"}

    log.info(
        "tokens_granted_from_payment",
        student_id=str(student.id),
        tokens=plan.tokens,
        payment_id=payment_id,
    )
    return {"status": "ok"}


def _apply_refund_from_webhook(db: DbSession, payment: Payment, entity: dict, event: dict) -> None:  # type: ignore[valid-type]
    refunded = int(entity.get("amount") or 0)
    payment.refunded_paise = int(payment.refunded_paise) + refunded
    payment.status = (
        "refunded"
        if payment.refunded_paise >= int(payment.amount_paise)
        else "partially_refunded"
    )
    payment.raw_webhook = event

    if payment.payment_id:
        taken = token_service.revoke(
            db,
            owner_type="student",
            owner_id=str(payment.student_id),
            qty=int(payment.tokens_granted or 0),
            reason=f"Refund of {payment.order_id}",
            actor_id=None,
            payment_id=payment.payment_id,
        )
        audit.record(
            db,
            action="payment.refunded",
            actor_id=None,
            actor_role="system",
            target=audit.target_for("payment", payment.payment_id),
            detail={"refunded_paise": refunded, "tokens_revoked": taken},
        )
    db.commit()


# ── Status (client callback) ──────────────────────────────────────────────────


@router.get("/status/{order_id}", response_model=PaymentStatusResponse)
async def payment_status(
    order_id: str, principal: CurrentUser, db: DbSession
) -> PaymentStatusResponse:
    """What the browser polls after Razorpay's checkout closes.

    Read-only on purpose. The old `/verify` endpoint both checked a signature and granted
    credits, which made it replayable; granting now happens only in the webhook.
    """
    payment = (
        db.query(Payment)
        .filter(Payment.order_id == order_id, Payment.student_id == principal.id)
        .first()
    )
    if payment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")

    message = {
        "created": "Payment is still being confirmed. This usually takes a few seconds.",
        "captured": f"Payment confirmed — {payment.tokens_granted} interview tokens added.",
        "failed": "That payment did not go through. You have not been charged.",
        "refunded": "This payment was refunded.",
        "partially_refunded": "This payment was partially refunded.",
    }.get(payment.status, payment.status)

    return PaymentStatusResponse(
        order_id=order_id,
        status=payment.status,
        tokens_granted=int(payment.tokens_granted or 0),
        message=message,
    )


@router.get("/history", response_model=list[PaymentStatusResponse])
async def payment_history(
    principal: CurrentUser, db: DbSession
) -> list[PaymentStatusResponse]:
    rows = (
        db.query(Payment)
        .filter(Payment.student_id == principal.id)
        .order_by(Payment.created_at.desc())
        .limit(50)
        .all()
    )
    return [
        PaymentStatusResponse(
            order_id=r.order_id,
            status=r.status,
            tokens_granted=int(r.tokens_granted or 0),
            message=r.plan_code,
        )
        for r in rows
    ]


# ── Admin refund ──────────────────────────────────────────────────────────────


@router.post("/refund", response_model=SimpleOk)
async def refund(
    body: RefundRequest, request: Request, principal: Admin, db: DbSession
) -> SimpleOk:
    """Admin-initiated refund. Revokes unused tokens from that purchase lot."""
    payment = (
        db.query(Payment).filter(Payment.payment_id == body.payment_id).first()
    )
    if payment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment not found")
    if payment.status not in ("captured", "partially_refunded"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Cannot refund a payment with status {payment.status}.",
        )

    amount = body.amount_paise or (
        int(payment.amount_paise) - int(payment.refunded_paise)
    )
    try:
        rzp.refund(payment_id=body.payment_id, amount_paise=amount)
    except rzp.RazorpayNotConfigured as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Payments are not configured."
        ) from exc
    except rzp.RazorpayError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "The payment provider refused the refund."
        ) from exc

    # Token revocation happens when the refund.processed webhook arrives, so the ledger
    # reflects what actually settled rather than what we asked for.
    audit.record(
        db,
        action="payment.refund_requested",
        actor_id=principal.id,
        actor_role=principal.role.value,
        target=audit.target_for("payment", body.payment_id),
        detail={"amount_paise": amount, "reason": body.reason},
        ip=ratelimit.client_ip(request),
    )
    db.commit()
    return SimpleOk(
        ok=True,
        message="Refund requested. Tokens are revoked when the provider confirms it.",
    )
