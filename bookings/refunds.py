"""
返金状態の整合性担保するモジュール

Stripe と LugGo の間で返金・キャンセル状態にズレが生じないようにする。
次の 3 経路のいずれから状態が変化しても、最終的に同じ結果へ収束させる。

1. 返金 API 成功後、アプリが落ちて delivery_status を更新できなかった
2. Stripe ダッシュボードからの手動返金（LugGo を経由しない）
3. 事業者キャンセル API のリトライ

いずれの場合も charge.refunded / refund.updated Webhook を受けて、
予約の返金状況・配達状況・監査ログを Stripe に合わせて更新する。
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from django.db import IntegrityError, transaction
from django.utils import timezone

from project.utils import mask_sensitive_id, stripe_get
from .models import BookingAuditLog, LuggageBooking, StripeWebhookEvent


logger = logging.getLogger(__name__)

# Stripe の refund.status を LugGo の refund_status へ正規化する対応表。
# requires_action は「まだ確定していない」ため pending と同義に扱う。
_STRIPE_REFUND_STATUS_MAP: dict[str, str] = {
    'succeeded': LuggageBooking.REFUND_STATUS_SUCCEEDED,
    'pending': LuggageBooking.REFUND_STATUS_PENDING,
    'requires_action': LuggageBooking.REFUND_STATUS_PENDING,
    'failed': LuggageBooking.REFUND_STATUS_FAILED,
    'canceled': LuggageBooking.REFUND_STATUS_CANCELED,
}

# refund_status ごとに対応する監査ログのアクション
_REFUND_STATUS_ACTION_MAP: dict[str, str] = {
    LuggageBooking.REFUND_STATUS_SUCCEEDED: BookingAuditLog.ACTION_REFUND_SUCCEEDED,
    LuggageBooking.REFUND_STATUS_PENDING: BookingAuditLog.ACTION_REFUND_PENDING,
    LuggageBooking.REFUND_STATUS_FAILED: BookingAuditLog.ACTION_REFUND_FAILED,
    LuggageBooking.REFUND_STATUS_CANCELED: BookingAuditLog.ACTION_REFUND_CANCELED,
}


def normalize_refund_status(stripe_status: Optional[str]) -> str:
    """Stripe の refund.status を LugGo の refund_status に変換（未知の値は pending 扱い）"""
    if not stripe_status:
        return LuggageBooking.REFUND_STATUS_PENDING
    return _STRIPE_REFUND_STATUS_MAP.get(
        stripe_status, LuggageBooking.REFUND_STATUS_PENDING
    )


def log_booking_event(
    *,
    booking: Optional[LuggageBooking],
    action: str,
    source: str,
    payment_intent_id: str = '',
    previous_delivery_status: str = '',
    new_delivery_status: str = '',
    previous_refund_status: str = '',
    new_refund_status: str = '',
    stripe_event_id: str = '',
    stripe_refund_id: str = '',
    stripe_charge_id: str = '',
    amount: int = 0,
    message: str = '',
    metadata: Optional[dict[str, Any]] = None,
) -> Optional[BookingAuditLog]:
    """
    予約の返金・キャンセルに関する監査ログを 1 件記録する（追記専用）

    監査ログの記録失敗が本処理（返金・キャンセル）を巻き戻すことがないよう、
    例外は握りつぶしてログ出力のみ行う。
    """
    pi = payment_intent_id or (booking.payment_intent_id if booking else '') or ''
    try:
        return BookingAuditLog.objects.create(
            booking=booking,
            payment_intent_id=pi,
            action=action,
            source=source,
            previous_delivery_status=previous_delivery_status or '',
            new_delivery_status=new_delivery_status or '',
            previous_refund_status=previous_refund_status or '',
            new_refund_status=new_refund_status or '',
            stripe_event_id=stripe_event_id or '',
            stripe_refund_id=stripe_refund_id or '',
            stripe_charge_id=stripe_charge_id or '',
            amount=amount if isinstance(amount, int) and amount >= 0 else 0,
            message=message or '',
            metadata=metadata or {},
        )
    except Exception:
        logger.exception(
            "監査ログの記録に失敗しました action=%s source=%s payment_intent_id=%s",
            action,
            source,
            mask_sensitive_id(pi) if pi else '-',
        )
        return None


def claim_webhook_event(event_id: str, event_type: str) -> bool:
    """
    返金 Webhook の二重実行を防ぐ

    - 初回受信 → True（処理してよい）
    - 処理済みの再送 → False（スキップ）
    """
    if not event_id:
        return True
    try:
        obj, _ = StripeWebhookEvent.objects.get_or_create(
            event_id=event_id,
            defaults={'event_type': event_type or ''},
        )
    except IntegrityError:
        # 競合で既に作成済み。改めて取得して未処理か確認。
        obj = StripeWebhookEvent.objects.filter(event_id=event_id).first()
        if obj is None:
            return True
    return obj.processed_at is None


def mark_webhook_event_processed(event_id: str) -> None:
    """Webhook イベントを処理完了として記録（以後の再送はスキップされる）"""
    if not event_id:
        return
    StripeWebhookEvent.objects.filter(event_id=event_id).update(
        processed_at=timezone.now()
    )


@dataclass
class ReconcileResult:
    """返金整合性処理の結果"""

    booking: Optional[LuggageBooking] = None
    matched: bool = False          # 予約が見つかったか
    changed: bool = False          # 予約レコードに変更が入ったか
    cancelled_now: bool = False    # 今回の処理でキャンセルに遷移したか
    changed_fields: list[str] = field(default_factory=list)


@transaction.atomic
def reconcile_booking_refund(
    *,
    payment_intent_id: str,
    refund_status: str,
    stripe_refund_id: str = '',
    stripe_charge_id: str = '',
    amount: int = 0,
    refunded_at: Any = None,
    source: str = BookingAuditLog.SOURCE_WEBHOOK,
    stripe_event_id: str = '',
) -> ReconcileResult:
    """
    Stripe 上の返金状態に合わせて予約の返金・配達状況を揃える（冪等）

    - 返金完了（succeeded）かつ未キャンセルなら、予約をキャンセル状態にする
      （＝返金 API 成功後にアプリが落ちたケース／ダッシュボード手動返金の復旧）
    - 返金状況 / 返金識別子 / 金額 / 完了日時を Stripe に合わせて補完する
    - 既に整合が取れている場合は何も変更しない（リトライ・再送に対して安全）

    予約が見つからない場合は matched=False を返す（呼び出し側で猶予リトライ・通知を判断）。
    """
    booking = (
        LuggageBooking.objects.select_for_update()
        .filter(payment_intent_id=payment_intent_id)
        .first()
    )
    if booking is None:
        return ReconcileResult(matched=False)

    prev_delivery = booking.delivery_status
    prev_refund = booking.refund_status
    update_fields: set[str] = set()

    # 返金識別子は分かる範囲で常に補完する（後からダッシュボードで参照できるように）
    if stripe_refund_id and booking.stripe_refund_id != stripe_refund_id:
        booking.stripe_refund_id = stripe_refund_id
        update_fields.add('stripe_refund_id')
    if stripe_charge_id and booking.stripe_charge_id != stripe_charge_id:
        booking.stripe_charge_id = stripe_charge_id
        update_fields.add('stripe_charge_id')

    cancelled_now = False

    if refund_status == LuggageBooking.REFUND_STATUS_SUCCEEDED:
        if booking.refund_status != LuggageBooking.REFUND_STATUS_SUCCEEDED:
            booking.refund_status = LuggageBooking.REFUND_STATUS_SUCCEEDED
            update_fields.add('refund_status')
        if amount and booking.refunded_amount != amount:
            booking.refunded_amount = amount
            update_fields.add('refunded_amount')
        if refunded_at and booking.refunded_at is None:
            booking.refunded_at = refunded_at
            update_fields.add('refunded_at')
        # 返金完了しているのに予約が未キャンセルなら、キャンセル済みにする
        if booking.delivery_status != 'cancelled':
            booking.delivery_status = 'cancelled'
            update_fields.add('delivery_status')
            cancelled_now = True
    else:
        # 返金未完了の通知では状況だけ更新するが、
        # 返金完了済みの予約は古いイベントで上書きしない
        if booking.refund_status != LuggageBooking.REFUND_STATUS_SUCCEEDED:
            if booking.refund_status != refund_status:
                booking.refund_status = refund_status
                update_fields.add('refund_status')

    if not update_fields:
        # 既に整合済み。監査ログにも変化を残さない。
        return ReconcileResult(
            booking=booking, matched=True, changed=False, cancelled_now=False
        )

    booking.refund_reconciled_at = timezone.now()
    update_fields.add('refund_reconciled_at')
    update_fields.add('updated_at')
    booking.save(update_fields=list(update_fields))

    action = _REFUND_STATUS_ACTION_MAP.get(
        refund_status, BookingAuditLog.ACTION_RECONCILED
    )
    log_booking_event(
        booking=booking,
        action=action,
        source=source,
        payment_intent_id=payment_intent_id,
        previous_delivery_status=prev_delivery,
        new_delivery_status=booking.delivery_status,
        previous_refund_status=prev_refund,
        new_refund_status=booking.refund_status,
        stripe_event_id=stripe_event_id,
        stripe_refund_id=booking.stripe_refund_id,
        stripe_charge_id=booking.stripe_charge_id,
        amount=amount,
        message='Stripe の返金状態に合わせて整合性を同期しました。',
        metadata={'stripe_refund_status': refund_status},
    )

    return ReconcileResult(
        booking=booking,
        matched=True,
        changed=True,
        cancelled_now=cancelled_now,
        changed_fields=sorted(update_fields),
    )


def record_api_refund_result(
    booking: LuggageBooking,
    refund: Any,
    *,
    source: str,
) -> None:
    """
    返金 API（stripe.Refund.create）の結果を予約へ反映し、監査ログに記録する

    キャンセル状態への遷移は呼び出し側（キャンセル API）が別途行う。ここでは返金の
    識別子・状態・金額を保存し、後続の Webhook 整合処理と突き合わせられるようにする。
    """
    if refund is None:
        return

    def _field(key: str) -> Any:
        return stripe_get(refund, key)

    refund_id = _field('id') or ''
    charge_id = _field('charge') or ''
    amount = _field('amount')
    amount = amount if isinstance(amount, int) and amount >= 0 else 0
    refund_status = normalize_refund_status(_field('status'))

    prev_refund = booking.refund_status
    update_fields: set[str] = set()

    if refund_id and booking.stripe_refund_id != refund_id:
        booking.stripe_refund_id = refund_id
        update_fields.add('stripe_refund_id')
    if charge_id and booking.stripe_charge_id != charge_id:
        booking.stripe_charge_id = charge_id
        update_fields.add('stripe_charge_id')
    if amount and booking.refunded_amount != amount:
        booking.refunded_amount = amount
        update_fields.add('refunded_amount')

    # 返金 API の返金状況を反映（返金完了済みの場合は変更しない）
    if booking.refund_status != LuggageBooking.REFUND_STATUS_SUCCEEDED:
        if booking.refund_status != refund_status:
            booking.refund_status = refund_status
            update_fields.add('refund_status')
    if (
        refund_status == LuggageBooking.REFUND_STATUS_SUCCEEDED
        and booking.refunded_at is None
    ):
        booking.refunded_at = timezone.now()
        update_fields.add('refunded_at')

    if update_fields:
        update_fields.add('updated_at')
        booking.save(update_fields=list(update_fields))

    action = _REFUND_STATUS_ACTION_MAP.get(
        refund_status, BookingAuditLog.ACTION_REFUND_REQUESTED
    )
    log_booking_event(
        booking=booking,
        action=action,
        source=source,
        payment_intent_id=booking.payment_intent_id,
        previous_refund_status=prev_refund,
        new_refund_status=booking.refund_status,
        stripe_refund_id=refund_id,
        stripe_charge_id=charge_id,
        amount=amount,
        message='返金APIを実行しました。',
        metadata={'stripe_refund_status': refund_status},
    )
