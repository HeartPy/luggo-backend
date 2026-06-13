"""
メール送信のトランスポート層（プロジェクト共通）

「どう送るか（transport）」だけを担い、「何を送るか（件名・本文の組み立て）」は
各ドメイン側（例: bookings/emails.py、users/utils.py）に委ねる。

送信経路は RESEND_API_KEY の有無で自動的に切り替える:
  - RESEND_API_KEY あり : Resend（https://resend.com/）の HTTP API を利用（本番想定）
  - RESEND_API_KEY なし : Django の EMAIL_BACKEND（send_mail）を利用（開発想定。
    既定は console バックエンドで標準出力に表示される）

これにより、認証・登録・予約確認・運営アラートなど全てのメールが、
本番では Resend、開発では EMAIL_BACKEND を通る。
"""

import logging
from typing import Optional
import requests
from django.conf import settings
from django.core.mail import send_mail


logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"

def operations_recipients() -> list[str]:
    """運営への通知先メールアドレス一覧を返す"""
    recipients = getattr(settings, "OPERATIONS_NOTIFICATION_EMAIL", "") or ""
    if isinstance(recipients, str):
        return [addr.strip() for addr in recipients.split(",") if addr.strip()]
    return [addr.strip() for addr in recipients if addr and addr.strip()]


def _resend_enabled() -> bool:
    """Resend を利用するか（API キーが設定されていれば本番送信に使う）"""
    return bool(getattr(settings, "RESEND_API_KEY", "") or "")


def _resolve_sender(from_email: Optional[str]) -> str:
    """送信元アドレスを決定"""
    return (
        from_email
        or getattr(settings, "RESEND_FROM_EMAIL", "")
        or settings.DEFAULT_FROM_EMAIL
    )


def _normalize_recipients(to: str | list[str]) -> list[str]:
    """宛先を空要素を除いた list[str] に正規化"""
    recipients = [to] if isinstance(to, str) else list(to)
    return [addr for addr in recipients if addr]


def send_email(
    *,
    subject: str,
    text: str,
    to: str | list[str],
    html: Optional[str] = None,
    from_email: Optional[str] = None,
) -> bool:
    """
    メールを送信する。送信に成功したら True を返す。

    RESEND_API_KEY が設定されていれば Resend、なければ Django の
    EMAIL_BACKENDを使って送信。
    """
    recipients = _normalize_recipients(to)
    if not recipients:
        logger.error(
            "送信先メールアドレスが空のためメール送信をスキップしました: subject=%s",
            subject,
        )
        return False

    sender = _resolve_sender(from_email)

    if _resend_enabled():
        return _send_via_resend(
            subject=subject,
            text=text,
            html=html,
            sender=sender,
            recipients=recipients,
        )

    return _send_via_django(
        subject=subject,
        text=text,
        html=html,
        sender=sender,
        recipients=recipients,
    )


def _send_via_resend(
    *,
    subject: str,
    text: str,
    html: Optional[str],
    sender: str,
    recipients: list[str],
) -> bool:
    """Resend の HTTP API でメールを送信"""
    api_key = getattr(settings, "RESEND_API_KEY", "") or ""

    payload: dict[str, str | list[str]] = {
        "from": sender,
        "to": recipients,
        "subject": subject,
        "text": text,
    }
    if html:
        payload["html"] = html

    try:
        response = requests.post(
            RESEND_API_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException as e:
        logger.error(
            "Resend メール送信に失敗しました: subject=%s, error=%s",
            subject,
            str(e),
            exc_info=True,
        )
        return False

    logger.info(
        "Resend メール送信に成功しました: subject=%s, recipients=%d",
        subject,
        len(recipients),
    )
    return True


def _send_via_django(
    *,
    subject: str,
    text: str,
    html: Optional[str],
    sender: str,
    recipients: list[str],
) -> bool:
    """Django の EMAIL_BACKEND（開発時は console など）でメールを送信"""
    try:
        send_mail(
            subject=subject,
            message=text,
            from_email=sender,
            recipient_list=recipients,
            html_message=html,
            fail_silently=False,
        )
    except Exception as e:
        logger.error(
            "メール送信に失敗しました（EMAIL_BACKEND）: subject=%s, error=%s",
            subject,
            str(e),
            exc_info=True,
        )
        return False

    logger.info(
        "メール送信に成功しました（EMAIL_BACKEND）: subject=%s, recipients=%d",
        subject,
        len(recipients),
    )
    return True
