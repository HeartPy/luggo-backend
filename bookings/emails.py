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
import re
from datetime import date as date_type
from typing import Any, Optional
import stripe
from django.template import TemplateDoesNotExist
from django.template.loader import render_to_string

from business_owners.models import BusinessProfile
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


def _format_phone_for_display(phone: str) -> str:
    """電話番号をメール表示用の国内形式に整形（+81... → 0...）"""
    phone = phone.strip()
    if not phone:
        return ""
    if phone.startswith("+"):
        digits = re.sub(r"\D", "", phone)
        if digits.startswith("81") and len(digits) > 2:
            return f"0{digits[2:]}"
    return phone


def _phone_from_user(profile: BusinessProfile) -> Optional[str]:
    """事業者ユーザーに登録された電話番号を返す"""
    user = getattr(profile, "user", None)
    if user and user.phone_number:
        formatted = _format_phone_for_display(user.phone_number)
        return formatted or None
    return None


def _format_address_kanji(addr: dict[str, Any]) -> str:
    """Stripe の address_kanji をプレーンテキスト用の住所文字列に整形"""
    parts = [
        addr.get("state", ""),
        addr.get("city", ""),
        addr.get("town", ""),
        addr.get("line1", ""),
        addr.get("line2", ""),
    ]
    address_body = "".join(part for part in parts if part)

    postal_raw = addr.get("postal_code", "")
    postal_half = (
        postal_raw.translate(str.maketrans("０１２３４５６７８９－", "0123456789-"))
        if postal_raw
        else ""
    )
    digits = "".join(char for char in postal_half if char.isdigit())
    if len(digits) == 7:
        postal_formatted = f"〒{digits[:3]}-{digits[3:]}"
    elif postal_half:
        postal_formatted = f"〒{postal_half}"
    else:
        postal_formatted = ""

    if postal_formatted and address_body:
        return f"{postal_formatted}\n{address_body}"
    if address_body:
        return address_body
    if postal_formatted:
        return postal_formatted
    return ""


def _enrich_business_signature_from_stripe(
    profile: BusinessProfile, signature: dict[str, Any]
) -> None:
    """Stripe Connect アカウントから所在地・問い合わせ先を補完"""
    account_id = profile.stripe_account_id
    if not account_id:
        return

    try:
        account = stripe.Account.retrieve(account_id)
    except stripe.error.StripeError:  # type: ignore[attr-defined]
        logger.warning(
            "予約確認メール: Stripeアカウント取得に失敗しました: business_owner_id=%s",
            profile.id,
        )
        return

    business_profile = account.get("business_profile") or {}
    support_email = business_profile.get("support_email")
    if support_email:
        signature["business_email"] = support_email

    support_phone = business_profile.get("support_phone")
    if support_phone:
        signature["business_phone"] = _format_phone_for_display(support_phone)

    business_type = account.get("business_type", "")
    if business_type == "company":
        addr = (account.get("company") or {}).get("address_kanji") or {}
    else:
        addr = (account.get("individual") or {}).get("address_kanji") or {}

    formatted_address = _format_address_kanji(addr)
    if formatted_address:
        signature["business_address"] = formatted_address

    if not signature.get("business_phone"):
        if business_type == "company":
            phone = (account.get("company") or {}).get("phone")
        else:
            phone = (account.get("individual") or {}).get("phone")
        if phone:
            signature["business_phone"] = _format_phone_for_display(phone)


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


def send_booking_cancellation_email_to_customer(booking: LuggageBooking) -> bool:
    """
    予約キャンセル時にユーザー（旅行者）へキャンセル通知メールを送信。送信成功で True。

    返金やステータス更新が完了した後に呼ぶこと。
    """
    if not booking.customer_email:
        logger.error(
            "キャンセル通知メールを送信できません（顧客メール未設定）: booking_id=%s",
            booking.id,
        )
        return False

    try:
        context = _booking_context(booking)
        subject, text, html = _render_email("booking_cancellation", context)
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


def send_booking_cancellation_emails(booking: LuggageBooking) -> None:
    """
    予約キャンセル時に顧客と担当配達者の双方へ通知メールを送信

    一方の送信失敗が他方を妨げないよう、それぞれ独立して送信する。
    """
    send_booking_cancellation_email_to_customer(booking)
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
