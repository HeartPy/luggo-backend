from django.utils import timezone
from django.conf import settings
from typing import Optional
import secrets
import logging

from project.email import send_email
from .models import RegistrationToken


logger = logging.getLogger(__name__)

def generate_registration_token(email: str) -> RegistrationToken:
    """登録用トークンを生成"""
    # 既存の有効なトークンがある場合は無効化
    RegistrationToken.objects.filter(
        email=email,
        used_at__isnull=True,
        expires_at__gt=timezone.now()
    ).update(used_at=timezone.now())

    # 新しいトークンを生成
    token = secrets.token_urlsafe(32)  # 32バイトのランダムトークン
    expires_at = timezone.now() + timezone.timedelta(minutes=30)

    registration_token = RegistrationToken.objects.create(
        email=email,
        token=token,
        expires_at=expires_at,
    )

    logger.info(f"登録トークン生成: email={email}, token_id={registration_token.id}")
    return registration_token


def send_registration_email(email: str, token: str) -> bool:
    """登録用メールを送信"""
    frontend_url = settings.FRONTEND_BASE_URL
    registration_url = f"{frontend_url}/account/register?token={token}"

    subject = "【LugGo】アカウント登録のご案内"
    message = f"""
この度は、LugGo（ラグゴー）へアカウント登録のお申し込みありがとうございます。

以下のリンクから、アカウント登録を完了してください。
このリンクは30分間のみ有効です。

{registration_url}

※このメールに心当たりがない場合は、このメールを無視してください。

---
LugGo（ラグゴー）
"""

    return send_email(subject=subject, text=message, to=email)


def verify_registration_token(token: str) -> Optional[RegistrationToken]:
    """登録トークンを検証"""
    try:
        registration_token = RegistrationToken.objects.get(token=token)
        if registration_token.is_valid():
            return registration_token
        return None
    except RegistrationToken.DoesNotExist:
        return None
