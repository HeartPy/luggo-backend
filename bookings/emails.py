"""
予約ドメインのメール送信を集約するモジュール

「何を・誰に送るか」をここに集約し、件名・本文は Django テンプレート
（bookings/templates/bookings/emails/）に切り出す。実際の配信は
project.email.send_email に委譲（Resend / EMAIL_BACKEND を自動切替）。

新しいメール種別を追加する場合は、
  1. templates/bookings/emails/<name>_subject.txt / <name>.txt /（任意で）<name>.html を追加
  2. ここに send_<name>_email(...) を追加
の2ステップで増やせる。
"""

import logging
from datetime import date as date_type
from typing import Any, Optional
from django.template import TemplateDoesNotExist
from django.template.loader import render_to_string

from business_owners.models import BusinessProfile
from business_owners.utils import build_booking_status_url
from business_owners.stripe_info import (
    format_phone_for_display,
    get_business_stripe_info,
)
from project.email import operations_recipients, send_email
from project.utils import mask_sensitive_id
from .models import LuggageBooking


logger = logging.getLogger(__name__)

# 荷物タイプのキーと表示名の対応（メール本文の表示に使用）
LUGGAGE_TYPE_LABELS: dict[str, str] = {
    "cabin": "機内持ち込みサイズ（3辺計：〜120cm）",
    "checked": "受託手荷物サイズ（3辺計：〜160cm）",
    "oversize": "規格外サイズ（3辺計：〜180cm）",
}

_TEMPLATE_DIR = "bookings/emails"


def _render_email(
    template_base: str, context: dict[str, Any]
) -> tuple[str, str, Optional[str]]:
    """
    件名・テキスト本文・HTML本文（任意）をテンプレートから生成

    - `<base>_subject.txt` : 件名（前後空白・改行は除去）
    - `<base>.txt`         : テキスト本文（必須）
    - `<base>.html`        : HTML本文（存在すれば使用、無ければ None）
    """
    subject = render_to_string(
        f"{_TEMPLATE_DIR}/{template_base}_subject.txt", context
    ).strip()
    body_text = render_to_string(f"{_TEMPLATE_DIR}/{template_base}.txt", context)

    body_html: Optional[str] = None
    try:
        body_html = render_to_string(f"{_TEMPLATE_DIR}/{template_base}.html", context)
    except TemplateDoesNotExist:
        body_html = None

    return subject, body_text, body_html


def _format_date(value: Optional[date_type]) -> str:
    """日付を「YYYY年MM月DD日」形式に整形"""
    if not value:
        return "-"
    return f"{value.year}年{value.month:02d}月{value.day:02d}日"


def _luggage_lines(luggage_items: Optional[dict[str, Any]]) -> list[dict[str, str | int]]:
    """荷物の {キー: 個数} を表示用の [{label, count}] に変換"""
    items = luggage_items or {}
    lines: list[dict[str, str | int]] = []
    for key, label in LUGGAGE_TYPE_LABELS.items():
        raw = items.get(key, 0)
        try:
            count = int(raw)
        except (TypeError, ValueError):
            count = 0
        if count > 0:
            lines.append({"label": label, "count": count})
    return lines


def _phone_from_user(profile: BusinessProfile) -> Optional[str]:
    """事業者ユーザーに登録された電話番号を返す"""
    user = getattr(profile, "user", None)
    if user and user.phone_number:
        formatted = format_phone_for_display(user.phone_number)
        return formatted or None
    return None


def _enrich_business_signature_from_stripe(
    profile: BusinessProfile, signature: dict[str, Any]
) -> None:
    """Stripe Connect アカウント（取得失敗時はキャッシュ）から所在地・問い合わせ先を補完"""
    info = get_business_stripe_info(profile)

    if info.get("support_email"):
        signature["business_email"] = info["support_email"]

    if info.get("business_address"):
        signature["business_address"] = info["business_address"]

    support_phone = info.get("support_phone") or ""
    account_phone = info.get("account_phone") or ""
    if support_phone:
        signature["business_phone"] = support_phone
    elif not signature.get("business_phone") and account_phone:
        signature["business_phone"] = account_phone


def _business_signature(profile: Optional[BusinessProfile]) -> dict[str, Any]:
    """フッター署名用の担当事業者情報を組み立て"""
    if profile is None:
        return {"business_name": None}

    signature: dict[str, Any] = {
        "business_name": profile.company_name,
        "business_address": None,
        "business_email": profile.company_email or None,
        "business_phone": _phone_from_user(profile),
    }
    _enrich_business_signature_from_stripe(profile, signature)
    return signature


def build_issuer_snapshot(profile: Optional[BusinessProfile]) -> dict[str, str]:
    """
    領収書の発行者情報スナップショット（名称・住所・連絡先）を組み立てる

    決済確定時点で Stripe から取得した発行者情報を予約レコードへ保存するために使う。
    後日 Stripe アカウントが変更・削除されても、この値で領収書を発行できる。
    """
    signature = _business_signature(profile)
    invoice_number = ""
    if profile is not None:
        invoice_number = (profile.invoice_registration_number or "").strip()
    return {
        "issuer_name": (signature.get("business_name") or "").strip(),
        "issuer_address": (signature.get("business_address") or "").strip(),
        "issuer_email": (signature.get("business_email") or "").strip(),
        "issuer_phone": (signature.get("business_phone") or "").strip(),
        "issuer_invoice_number": invoice_number,
    }


def _booking_context(booking: LuggageBooking) -> dict[str, Any]:
    """予約レコードからメールテンプレート用のコンテキストを組み立て"""
    profile = booking.business_owner
    context: dict[str, Any] = {
        "booking_number": booking.booking_number,
        "customer_name": booking.customer_name,
        "pickup_location_name": booking.pickup_location_name,
        "pickup_location_address": booking.pickup_location_address,
        "pickup_date": _format_date(booking.pickup_date),
        "delivery_location_name": booking.delivery_location_name,
        "delivery_location_address": booking.delivery_location_address,
        "delivery_date": _format_date(booking.delivery_date),
        "luggage_lines": _luggage_lines(booking.luggage_items),
        "total_amount_display": f"{booking.total_amount:,}",
        "notes": booking.notes,
        "status_url": (
            build_booking_status_url(profile.subdomain)
            if profile is not None and profile.subdomain
            else None
        ),
    }
    context.update(_business_signature(profile))
    return context


def send_booking_confirmation_email(booking: LuggageBooking) -> bool:
    """
    予約確定時に顧客へ予約確認メールを送信。送信成功で True。

    予約レコードが DB に保存された後に呼ぶこと（予約番号など完全な情報を含めるため）。
    """
    if not booking.customer_email:
        logger.error(
            "予約確認メールを送信できません（顧客メール未設定）: booking_id=%s",
            booking.id,
        )
        return False

    try:
        context = _booking_context(booking)
        subject, text, html = _render_email("booking_confirmation", context)
    except Exception:
        logger.exception(
            "予約確認メールの組み立てに失敗しました: booking_id=%s", booking.id
        )
        return False

    return send_email(
        subject=subject,
        text=text,
        to=booking.customer_email,
        html=html,
    )


def _driver_recipient(booking: LuggageBooking) -> tuple[Optional[str], str]:
    """キャンセル通知の宛先となる配達者のメールと表示名を返す"""
    driver = booking.driver
    if driver is None or not driver.user_id:
        return None, ""

    user = driver.user
    email = (getattr(user, "email", "") or "").strip()
    name = (user.get_full_name() or "").strip()
    if not name:
        company_name = (driver.company_name or "").strip()
        name = f"{company_name}\nご担当者" if company_name else "ご担当者"
    return (email or None), name


def send_booking_cancellation_email_to_customer(
    booking: LuggageBooking, refunded: bool = True
) -> bool:
    """
    予約キャンセル時にユーザー（旅行者）へキャンセル通知メールを送信。送信成功で True。

    `refunded` が True の場合は返金あり、False の場合は返金なしの文面を送信する。
    返金やステータス更新が完了した後に呼ぶこと。
    """
    if not booking.customer_email:
        logger.error(
            "キャンセル通知メールを送信できません（顧客メール未設定）: booking_id=%s",
            booking.id,
        )
        return False

    template_base = (
        "booking_cancellation" if refunded else "booking_cancellation_no_refund"
    )
    try:
        context = _booking_context(booking)
        subject, text, html = _render_email(template_base, context)
    except Exception:
        logger.exception(
            "顧客向けキャンセル通知メールの組み立てに失敗しました: booking_id=%s",
            booking.id,
        )
        return False

    return send_email(
        subject=subject,
        text=text,
        to=booking.customer_email,
        html=html,
    )


def send_booking_cancellation_email_to_driver(booking: LuggageBooking) -> bool:
    """予約キャンセル時に担当配達者へキャンセル通知メールを送信。送信成功で True。"""
    email, driver_name = _driver_recipient(booking)
    if not email:
        logger.info(
            "配達者向けキャンセル通知をスキップしました（宛先なし）: booking_id=%s",
            booking.id,
        )
        return False

    try:
        context = _booking_context(booking)
        context["driver_name"] = driver_name
        subject, text, html = _render_email("booking_cancellation_driver", context)
    except Exception:
        logger.exception(
            "配達者向けキャンセル通知メールの組み立てに失敗しました: booking_id=%s",
            booking.id,
        )
        return False

    return send_email(
        subject=subject,
        text=text,
        to=email,
        html=html,
    )


def send_booking_cancellation_emails(
    booking: LuggageBooking, refunded: bool = True
) -> None:
    """
    予約キャンセル時に顧客と担当配達者の双方へ通知メールを送信

    一方の送信失敗が他方を妨げないよう、それぞれ独立して送信する。
    """
    send_booking_cancellation_email_to_customer(booking, refunded=refunded)
    send_booking_cancellation_email_to_driver(booking)


def send_unmatched_payment_alert(
    *,
    payment_intent_id: str,
    amount: Optional[int],
    customer_name: Optional[str] = None,
    customer_email: Optional[str] = None,
    business_name: Optional[str] = None,
    created_iso: Optional[str] = None,
) -> bool:
    """
    決済成功済みだが対応する予約レコードが無いことを運営へ通知。

    返金または手動での予約登録の要否を運営が判断できるよう、
    PaymentIntent の情報と Stripe ダッシュボードへのリンクを本文に含める。
    """
    recipients = operations_recipients()
    if not recipients:
        logger.error(
            "OPERATIONS_NOTIFICATION_EMAIL が未設定のため未照合決済アラートを送信できません: "
            "payment_intent_id=%s",
            mask_sensitive_id(payment_intent_id),
        )
        return False

    context = {
        "payment_intent_id": payment_intent_id,
        "amount_display": f"¥{amount:,}" if isinstance(amount, int) else "-",
        "customer_name": customer_name or "-",
        "customer_email": customer_email or "-",
        "business_name": business_name or "-",
        "created_iso": created_iso or "-",
        "dashboard_url": f"https://dashboard.stripe.com/payments/{payment_intent_id}",
    }

    try:
        subject, text, html = _render_email("unmatched_payment_alert", context)
    except Exception:
        logger.exception(
            "未照合決済アラートの組み立てに失敗しました: payment_intent_id=%s",
            mask_sensitive_id(payment_intent_id),
        )
        return False

    return send_email(subject=subject, text=text, to=recipients, html=html)


# Stripe の dispute.reason と日本語表示の対応
DISPUTE_REASON_LABELS: dict[str, str] = {
    "bank_cannot_process": "銀行が処理できない",
    "check_returned": "小切手の返却",
    "credit_not_processed": "クレジットが処理されていない",
    "customer_initiated": "顧客都合による申立て",
    "debit_not_authorized": "デビットが承認されていない",
    "duplicate": "二重請求",
    "fraudulent": "不正利用の疑い",
    "general": "その他（一般）",
    "incorrect_account_details": "口座情報の誤り",
    "insufficient_funds": "残高不足",
    "product_not_received": "商品・サービスの未受領",
    "product_unacceptable": "商品・サービスへの不満",
    "subscription_canceled": "サブスクリプションの解約",
    "unrecognized": "身に覚えのない請求",
}

# Stripe の dispute.status と日本語表示の対応
DISPUTE_STATUS_LABELS: dict[str, str] = {
    "warning_needs_response": "警告：要対応",
    "warning_under_review": "警告：審査中",
    "warning_closed": "警告：クローズ",
    "needs_response": "要対応（証拠提出が必要）",
    "under_review": "審査中",
    "won": "勝訴（資金は返還されません）",
    "lost": "敗訴（資金が引き落とされました）",
    "charge_refunded": "返金済み",
}


def _dispute_reason_label(reason: Optional[str]) -> str:
    """dispute.reason を日本語表示に変換（未知の値はそのまま返す）"""
    if not reason:
        return "-"
    return DISPUTE_REASON_LABELS.get(reason, reason)


def _dispute_status_label(status_value: Optional[str]) -> str:
    """dispute.status を日本語表示に変換（未知の値はそのまま返す）"""
    if not status_value:
        return "-"
    return DISPUTE_STATUS_LABELS.get(status_value, status_value)


def send_charge_dispute_alert(
    *,
    event_type: str,
    dispute_id: str,
    payment_intent_id: Optional[str] = None,
    charge_id: Optional[str] = None,
    amount: Optional[int] = None,
    currency: Optional[str] = None,
    reason: Optional[str] = None,
    status_value: Optional[str] = None,
    is_charge_refundable: Optional[bool] = None,
    evidence_due_iso: Optional[str] = None,
    opened_iso: Optional[str] = None,
    booking_number: Optional[str] = None,
    customer_name: Optional[str] = None,
    business_name: Optional[str] = None,
) -> bool:
    """
    チャージバック（異議申立て）の発生・クローズを運営へ通知

    charge.dispute.created / charge.dispute.closed のいずれでも呼ばれる。
    証拠提出期限や対応する予約・事業者を本文に含め、運営が対応要否を判断できるようにする。
    """
    recipients = operations_recipients()
    if not recipients:
        logger.error(
            "OPERATIONS_NOTIFICATION_EMAIL が未設定のためチャージバック通知を送信できません: "
            "dispute_id=%s",
            mask_sensitive_id(dispute_id),
        )
        return False

    is_closed = event_type == "charge.dispute.closed"

    if isinstance(amount, int):
        currency_code = (currency or "").upper()
        # JPY は最小単位＝円のため係数変換しない。それ以外は 100 で割って表示。
        if (currency or "").lower() == "jpy" or not currency:
            amount_display = f"¥{amount:,}"
        else:
            amount_display = f"{amount / 100:,.2f} {currency_code}"
    else:
        amount_display = "-"

    context = {
        "is_closed": is_closed,
        "dispute_id": dispute_id,
        "payment_intent_id": payment_intent_id or "-",
        "charge_id": charge_id or "-",
        "amount_display": amount_display,
        "reason_display": _dispute_reason_label(reason),
        "status_display": _dispute_status_label(status_value),
        "is_charge_refundable": bool(is_charge_refundable),
        "evidence_due_iso": evidence_due_iso or "-",
        "opened_iso": opened_iso or "-",
        "booking_number": booking_number or "-",
        "customer_name": customer_name or "-",
        "business_name": business_name or "-",
        "dashboard_url": f"https://dashboard.stripe.com/disputes/{dispute_id}",
    }

    try:
        subject, text, html = _render_email("charge_dispute_alert", context)
    except Exception:
        logger.exception(
            "チャージバック通知の組み立てに失敗しました: dispute_id=%s",
            mask_sensitive_id(dispute_id),
        )
        return False

    return send_email(subject=subject, text=text, to=recipients, html=html)
