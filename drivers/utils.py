"""配達者登録の確認メールとワンタイムトークンを扱う"""

import hashlib
import secrets
from typing import Optional

from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone

from project.email import email_signature, send_email
from .models import DriverInvitation


INVITATION_VALID_DAYS = 7


def token_digest(token: str) -> str:
    """生トークンをDB検索用のSHA-256のハッシュ値へ変換"""
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def create_invitation_token() -> tuple[str, str]:
    """メール用の生トークンとDB保存用ハッシュ値を返す"""
    token = secrets.token_urlsafe(32)
    return token, token_digest(token)


def verify_driver_invitation(token: str) -> Optional[DriverInvitation]:
    """有効な配達者招待を返す。生トークンはログやDBへ保存しない。"""
    if not token:
        return None
    try:
        invitation = DriverInvitation.objects.select_related(
            'business_owner'
        ).get(token_digest=token_digest(token))
    except DriverInvitation.DoesNotExist:
        return None
    return invitation if invitation.is_valid() else None


def send_driver_invitation_email(invitation: DriverInvitation, token: str) -> bool:
    """配達者本人へ登録確認メールを送信"""
    context = {
        'accept_url': (
            f'{settings.FRONTEND_BASE_URL}/driver/invitation/accept?token={token}'
        ),
        'driver_name': f'{invitation.last_name} {invitation.first_name}'.strip(),
        'owner_company_name': invitation.business_owner.company_name,
        'expires_days': INVITATION_VALID_DAYS,
        'signature': email_signature(),
    }
    subject = render_to_string(
        'drivers/emails/driver_invitation_subject.txt', context
    ).strip()
    text = render_to_string('drivers/emails/driver_invitation.txt', context)
    return send_email(subject=subject, text=text, to=invitation.email)


def invitation_expiry():
    return timezone.now() + timezone.timedelta(days=INVITATION_VALID_DAYS)
