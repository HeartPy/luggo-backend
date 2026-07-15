"""
配達完了した予約の売上を事業者（連結アカウント）へ送金する処理

決済はプラットフォームアカウントで受け付け（destination charge を使わない）、
配達完了（delivered）になった時点で Stripe Transfer により、プラットフォーム
手数料を差し引いた金額を事業者の連結アカウントへ送金する。

Transfer に source_transaction（決済の Charge）を紐付けることで、送金は
その決済資金が Stripe 上で入金可能になるのを待ってから実行されるため、
プラットフォーム残高の不足による送金失敗を防げる。
"""

import logging
from datetime import datetime, timezone as dt_timezone
from typing import Any, Optional

import stripe
from django.conf import settings
from django.utils import timezone

from project.utils import mask_sensitive_id
from business_owners.revenue import platform_fee, refunded_platform_fee
from .models import BookingAuditLog, LuggageBooking
from .refunds import log_booking_event


stripe.api_key = settings.STRIPE_SECRET_KEY
logger = logging.getLogger(__name__)


def _get(value: Any, key: str, default: Any = None) -> Any:
    if value is None:
        return default
    if hasattr(value, "get"):
        return value.get(key, default)
    return getattr(value, key, default)


def transfer_payout_amount(booking: LuggageBooking) -> int:
    """送金額 =（合計金額 − 返金済み金額）− 差引後のプラットフォーム手数料"""
    total = int(booking.total_amount or 0)
    refunded = min(total, int(booking.refunded_amount or 0))
    fee = platform_fee(total) - refunded_platform_fee(total, refunded)
    return (total - refunded) - fee


def _funds_available_on(charge: Any) -> Optional[datetime]:
    """Charge の balance_transaction から資金が入金可能になる日時を取得"""
    balance_transaction = _get(charge, "balance_transaction")
    available_on = _get(balance_transaction, "available_on")
    try:
        return datetime.fromtimestamp(int(available_on), tz=dt_timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def create_transfer_for_delivered_booking(booking: LuggageBooking) -> bool:
    """
    配達完了した予約の売上を事業者の連結アカウントへ送金（冪等）

    送金済みの場合は何もしない。Stripe エラー時は False を返すだけで、
    呼び出し元の処理（配達状況の保存）は止めない。次回の配達状況保存時に
    再試行される。
    """
    if booking.stripe_transfer_id:
        return True
    if booking.delivery_status != 'delivered':
        return False

    payment_intent_id = (booking.payment_intent_id or '').strip()
    business = booking.business_owner
    account_id = (getattr(business, 'stripe_account_id', '') or '').strip()
    if not payment_intent_id or not account_id:
        logger.error(
            "送金不可: 決済情報または連結アカウントがありません booking_id=%s",
            mask_sensitive_id(str(booking.id)),
        )
        return False

    try:
        payment_intent = stripe.PaymentIntent.retrieve(
            payment_intent_id,
            expand=['latest_charge.balance_transaction'],
        )
    except stripe.error.StripeError as e:
        logger.warning(
            "送金前の決済情報取得に失敗: booking_id=%s error=%s",
            mask_sensitive_id(str(booking.id)),
            str(e),
        )
        return False

    charge = _get(payment_intent, 'latest_charge')
    charge_id = str(_get(charge, 'id', '') or '')
    if not charge_id:
        logger.error(
            "送金不可: 決済の Charge が見つかりません booking_id=%s",
            mask_sensitive_id(str(booking.id)),
        )
        return False

    update_fields: list[str] = []
    available_on = _funds_available_on(charge)
    if available_on and booking.funds_available_on != available_on:
        booking.funds_available_on = available_on
        update_fields.append('funds_available_on')
    if not booking.stripe_charge_id:
        booking.stripe_charge_id = charge_id
        update_fields.append('stripe_charge_id')

    amount = transfer_payout_amount(booking)
    if amount <= 0:
        logger.info(
            "送金対象額が 0 円以下のためスキップ: booking_id=%s",
            mask_sensitive_id(str(booking.id)),
        )
        return False

    try:
        transfer = stripe.Transfer.create(
            amount=amount,
            currency='jpy',
            destination=account_id,
            # 決済の Charge に紐付け、資金が入金可能になってから送金する
            source_transaction=charge_id,
            metadata={'booking_id': str(booking.id)},
            # 同一予約への二重送金を防ぐ（再試行時も同じ結果が返る）
            idempotency_key=f'booking_delivery_transfer_{booking.id}',
        )
    except stripe.error.StripeError as e:
        logger.warning(
            "事業者への送金に失敗: booking_id=%s error=%s",
            mask_sensitive_id(str(booking.id)),
            str(e),
        )
        return False

    booking.stripe_transfer_id = str(_get(transfer, 'id', '') or '')
    booking.transferred_at = timezone.now()
    update_fields.extend(['stripe_transfer_id', 'transferred_at', 'updated_at'])
    booking.save(update_fields=update_fields)

    log_booking_event(
        booking=booking,
        action=BookingAuditLog.ACTION_TRANSFER_CREATED,
        source=BookingAuditLog.SOURCE_OWNER_API,
        stripe_charge_id=charge_id,
        amount=amount,
        message='配達完了により事業者への送金を実行しました。',
        metadata={'stripe_transfer_id': booking.stripe_transfer_id},
    )
    logger.info(
        "事業者への送金を作成: booking_id=%s amount=%s",
        mask_sensitive_id(str(booking.id)),
        amount,
    )
    return True
