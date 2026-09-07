"""
メール送信のトランスポート層（プロジェクト共通）

「どう送るか（transport）」だけを担い、「何を送るか（件名・本文の組み立て）」は
各ドメイン側（例: bookings/emails.py、users/utils.py）に委ねる。

送信経路は RESEND_API_KEY の有無で自動的に切り替える:
  - RESEND_API_KEY あり : Resend（https://resend.com/）の HTTP API を利用（本番想定）
  - RESEND_API_KEY なし : Django の console EmailBackend（開発想定。標準出力に表示）

これにより、認証・登録・予約確認・運営アラートなど全てのメールが、
本番では Resend、開発ではコンソール出力を通る。
"""

import base64
import logging
from typing import Optional
import requests
from django.conf import settings
from django.core.mail import EmailMultiAlternatives


logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"

LUGO_SUPPORT_EMAIL = "support@luggo.delivery"


def operations_recipients() -> list[str]:
    """運営への通知先メールアドレス一覧を返す"""
    recipients = getattr(settings, "OPERATIONS_NOTIFICATION_EMAIL", "") or ""
    if isinstance(recipients, str):
        return [addr.strip() for addr in recipients.split(",") if addr.strip()]
    return [addr.strip() for addr in recipients if addr and addr.strip()]


def email_signature() -> str:
    """LugGo（運営）から送るメールに付与する共通の署名"""
    frontend_url = getattr(settings, "FRONTEND_BASE_URL", "") or ""
    website_line = f"Webサイト：{frontend_url}\n" if frontend_url else ""
    return (
        "※このメールは送信専用です。ご返信いただいてもお答えできません。\n\n"
        "----------------------------------------\n"
        "LugGo（ラグゴー）運営事務局\n"
        f"お問い合わせ：{LUGO_SUPPORT_EMAIL}\n"
        f"{website_line}"
        "----------------------------------------"
    )


def _resend_enabled() -> bool:
    """Resend を利用するか（API キーが設定されていれば本番送信に使う）"""
    return bool(getattr(settings, "RESEND_API_KEY", "") or "")


def format_from_header(display_name: str, address: str) -> str:
    """表示名付きの From ヘッダを組み立てる。表示名が空ならアドレスのみ。"""
    addr = (address or "").strip()
    name = (display_name or "").strip()
    if not name:
        return addr
    if any(ch in name for ch in '()<>@,;:\\".[]'):
        escaped = name.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}" <{addr}>'
    return f"{name} <{addr}>"


def platform_from_header() -> str:
    """運営として送るときの From（表示名 + DEFAULT_FROM_EMAIL）"""
    display = getattr(settings, "PLATFORM_FROM_DISPLAY_NAME", "") or "LugGo(ラグゴー)"
    return format_from_header(display, settings.DEFAULT_FROM_EMAIL)


def _resolve_sender(from_email: Optional[str]) -> str:
    """送信元を決定。未指定なら運営の表示名付き From。"""
    return from_email or platform_from_header()


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
    reply_to: Optional[str | list[str]] = None,
    attachments: Optional[list[tuple[str, bytes, str]]] = None,
) -> bool:
    """
    メールを送信する。送信に成功したら True を返す。

    RESEND_API_KEY が設定されていれば Resend、なければ Django の
    console EmailBackend で出力。
    from_email 未指定時は運営の表示名付きアドレスを使う。
    """
    recipients = _normalize_recipients(to)
    if not recipients:
        logger.error(
            "送信先メールアドレスが空のためメール送信をスキップしました: subject=%s",
            subject,
        )
        return False

    sender = _resolve_sender(from_email)
    reply_to_list = _normalize_recipients(reply_to) if reply_to else []

    if _resend_enabled():
        return _send_via_resend(
            subject=subject,
            text=text,
            html=html,
            sender=sender,
            recipients=recipients,
            reply_to=reply_to_list,
            attachments=attachments or [],
        )

    return _send_via_django(
        subject=subject,
        text=text,
        html=html,
        sender=sender,
        recipients=recipients,
        reply_to=reply_to_list,
        attachments=attachments or [],
    )


def _send_via_resend(
    *,
    subject: str,
    text: str,
    html: Optional[str],
    sender: str,
    recipients: list[str],
    reply_to: list[str],
    attachments: list[tuple[str, bytes, str]],
) -> bool:
    """Resend の HTTP API でメールを送信"""
    api_key = getattr(settings, "RESEND_API_KEY", "") or ""

    payload: dict[str, object] = {
        "from": sender,
        "to": recipients,
        "subject": subject,
        "text": text,
    }
    if reply_to:
        payload["reply_to"] = reply_to
    if html:
        payload["html"] = html
    if attachments:
        payload["attachments"] = [
            {
                "filename": filename,
                "content": base64.b64encode(content).decode("ascii"),
            }
            for filename, content, _content_type in attachments
        ]

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
    reply_to: list[str],
    attachments: list[tuple[str, bytes, str]],
) -> bool:
    """Django の mail（開発時は console）でメールを送信"""
    try:
        message = EmailMultiAlternatives(
            subject=subject,
            body=text,
            from_email=sender,
            to=recipients,
            reply_to=reply_to or None,
        )
        if html:
            message.attach_alternative(html, "text/html")
        for filename, content, content_type in attachments:
            message.attach(filename, content, content_type)
        message.send(fail_silently=False)
    except Exception as e:
        logger.error(
            "メール送信に失敗しました（console）: subject=%s, error=%s",
            subject,
            str(e),
            exc_info=True,
        )
        return False

    logger.info(
        "メール送信に成功しました（console）: subject=%s, recipients=%d",
        subject,
        len(recipients),
    )
    return True
