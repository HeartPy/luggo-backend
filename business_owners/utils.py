from django.utils import timezone
from django.conf import settings
from django.template.loader import render_to_string
from typing import Any, Optional, TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit
import secrets
import logging

from project.email import send_email, operations_recipients, email_signature
from .models import RegistrationToken

if TYPE_CHECKING:
    from .models import BusinessProfile


logger = logging.getLogger(__name__)

BUSINESS_TYPE_LABELS = {
    'company': '法人',
    'individual': '個人事業主',
}

_EMAIL_TEMPLATE_DIR = "business_owners/emails"

def _render_email(template_base: str, context: dict[str, Any]) -> tuple[str, str]:
    """件名・テキスト本文をテンプレートから生成"""
    subject = render_to_string(
        f"{_EMAIL_TEMPLATE_DIR}/{template_base}_subject.txt", context
    ).strip()
    body_text = render_to_string(f"{_EMAIL_TEMPLATE_DIR}/{template_base}.txt", context)
    return subject, body_text


# フロントの TENANT_BASE_DOMAIN と同じ。開発時 FRONTEND_BASE_URL が localhost
# でも Stripe 向け公開 URL を組み立てるために使う。
TENANT_BASE_DOMAIN = "luggo.delivery"


def build_booking_form_url(subdomain: str) -> str:
    """旅行者向け予約フォームの URL を組み立て"""
    base = settings.FRONTEND_BASE_URL
    parts = urlsplit(base)
    protocol = parts.scheme or "https"
    hostname = parts.hostname or ""
    port = parts.port

    # 開発環境（localhost / 127.0.0.1）はサブドメインをクエリパラメータで渡す
    if hostname in ("localhost", "127.0.0.1"):
        port_str = f":{port}" if port else ""
        return f"{protocol}://{hostname}{port_str}/booking?subdomain={subdomain}"

    # 本番環境はホスト名の先頭にサブドメインを付与する
    host_parts = hostname.split(".")
    base_domain = ".".join(host_parts[1:]) if len(host_parts) >= 3 else hostname
    return f"{protocol}://{subdomain}.{base_domain}/booking"


def build_stripe_business_profile_url(subdomain: str) -> str:
    """Stripe business_profile.url 向けの公開テナント URL を組み立て"""
    return f"https://{subdomain}.{TENANT_BASE_DOMAIN}"


def resolve_stripe_business_profile_url(
    url: str, subdomain: Optional[str] = None
) -> str:
    """
    Stripe に送る business_profile.url を解決

    Stripe は localhost / 127.0.0.1 を拒否するため、開発用 URL の場合は
    公開ドメイン形式（https://{subdomain}.luggo.delivery）へ置き換える。
    """
    stripped = (url or "").strip()
    if not stripped:
        return stripped

    parts = urlsplit(stripped)
    hostname = (parts.hostname or "").lower()
    if hostname not in ("localhost", "127.0.0.1"):
        return stripped

    resolved_subdomain = (subdomain or "").strip().lower()
    if not resolved_subdomain:
        # 開発用 URL の ?subdomain= からも拾う
        query = parse_qs(parts.query)
        candidates = query.get("subdomain") or []
        if candidates and isinstance(candidates[0], str):
            resolved_subdomain = candidates[0].strip().lower()

    if not resolved_subdomain:
        return stripped

    return build_stripe_business_profile_url(resolved_subdomain)


def build_booking_status_url(subdomain: str) -> str:
    """旅行者向け予約状況（確認・キャンセル）ページの URL を組み立て"""
    base = settings.FRONTEND_BASE_URL
    parts = urlsplit(base)
    protocol = parts.scheme or "https"
    hostname = parts.hostname or ""
    port = parts.port

    # 開発環境（localhost / 127.0.0.1）はサブドメインをクエリパラメータで渡す
    if hostname in ("localhost", "127.0.0.1"):
        port_str = f":{port}" if port else ""
        return f"{protocol}://{hostname}{port_str}/booking/status?subdomain={subdomain}"

    # 本番環境はホスト名の先頭にサブドメインを付与する
    host_parts = hostname.split(".")
    base_domain = ".".join(host_parts[1:]) if len(host_parts) >= 3 else hostname
    return f"{protocol}://{subdomain}.{base_domain}/booking/status"


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
    context = {
        "registration_url": (
            f"{settings.FRONTEND_BASE_URL}/account/register?token={token}"
        ),
        "signature": email_signature(),
    }
    subject, text = _render_email("registration_invitation", context)
    return send_email(subject=subject, text=text, to=email)


def verify_registration_token(token: str) -> Optional[RegistrationToken]:
    """登録トークンを検証"""
    try:
        registration_token = RegistrationToken.objects.get(token=token)
        if registration_token.is_valid():
            return registration_token
        return None
    except RegistrationToken.DoesNotExist:
        return None


def owner_email_addressee_context(profile: "BusinessProfile") -> dict[str, Any]:
    """事業者向けメールの宛名用コンテキスト（法人のみ会社名を表示）"""
    return {
        "is_company": profile.business_type == "company",
        "company_name": (profile.company_name or "").strip(),
        "rep_name": (
            f"{profile.rep_last_name_kanji} {profile.rep_first_name_kanji}".strip()
        ),
    }


def _registration_email_context(profile: "BusinessProfile") -> dict[str, Any]:
    """登録完了通知メールのテンプレートに渡す共通コンテキストを組み立て"""
    user = profile.user
    context = {
        "business_type_label": BUSINESS_TYPE_LABELS.get(
            profile.business_type, profile.business_type
        ),
        "email": user.email,
        "phone_number": user.phone_number,
        "booking_url": build_booking_form_url(profile.subdomain),
        "signature": email_signature(),
    }
    context.update(owner_email_addressee_context(profile))
    return context


def send_registration_completed_email_to_owner(profile: "BusinessProfile") -> bool:
    """登録を完了した事業者へ、登録完了の通知メールを送信"""
    user = profile.user
    context = _registration_email_context(profile)
    context["login_url"] = f"{settings.FRONTEND_BASE_URL}/account/login"

    subject, text = _render_email("registration_completed_owner", context)
    return send_email(subject=subject, text=text, to=user.email)


def send_registration_completed_email_to_operations(profile: "BusinessProfile") -> bool:
    """事業者の登録完了を運営へ通知"""
    recipients = operations_recipients()
    if not recipients:
        logger.error(
            "OPERATIONS_NOTIFICATION_EMAIL が未設定のため事業者登録完了の運営通知を送信できません: "
            "company_name=%s",
            profile.company_name,
        )
        return False

    context = _registration_email_context(profile)
    context["registered_at"] = timezone.localtime(profile.created_at).strftime(
        "%Y年%m月%d日 %H:%M"
    )

    subject, text = _render_email("registration_completed_operations", context)
    return send_email(subject=subject, text=text, to=recipients)


def send_registration_completed_emails(profile: "BusinessProfile") -> None:
    """事業者登録完了時に、事業者本人と運営の双方へ通知メールを送信

    一方の送信失敗が他方を妨げないよう、それぞれ独立して送信する。
    """
    send_registration_completed_email_to_owner(profile)
    send_registration_completed_email_to_operations(profile)


# Stripe 審査状態ごとの、事業者向けメールに表示する日本語ラベルと説明文
_STRIPE_REVIEW_STATUS_LABELS: dict[str, str] = {
    "unknown": "未取得",
    "incomplete": "入力未完了",
    "pending": "審査中",
    "restricted": "要対応",
    "enabled": "利用可能",
    "rejected": "利用不可",
}

_STRIPE_REVIEW_STATUS_MESSAGES: dict[str, str] = {
    "pending": (
        "決済アカウントの審査が開始されました。\n"
        "審査完了までしばらくお待ちください。結果は改めてメールでお知らせします。"
    ),
    "restricted": (
        "決済アカウントの審査を進めるために、追加のご対応が必要です。\n"
        "ダッシュボードから不足している情報・書類をご確認のうえ、ご提出ください。"
    ),
    "enabled": (
        "決済アカウントの審査が完了し、ご利用いただけるようになりました。\n"
        "予約の受付・決済が可能です。"
    ),
    "rejected": (
        "決済アカウントがご利用いただけない状態になりました。\n"
        "お手数ですが、詳細については運営事務局までお問い合わせください。"
    ),
}


def send_stripe_review_status_email(
    profile: "BusinessProfile", old_status: str, new_status: str
) -> bool:
    """Stripe 審査状態が変化した際に、事業者本人へ通知メールを送信"""
    user = profile.user
    to_email = getattr(user, "email", "") or profile.company_email
    if not to_email:
        logger.error(
            "宛先メールアドレスが無いため Stripe 審査状態通知を送信できません: profile_id=%s",
            profile.id,
        )
        return False

    context = {
        "old_status_label": _STRIPE_REVIEW_STATUS_LABELS.get(old_status, old_status),
        "new_status_label": _STRIPE_REVIEW_STATUS_LABELS.get(new_status, new_status),
        "status_message": _STRIPE_REVIEW_STATUS_MESSAGES.get(new_status, ""),
        "dashboard_url": f"{settings.FRONTEND_BASE_URL}/business-owner/dashboard",
        "signature": email_signature(),
    }
    context.update(owner_email_addressee_context(profile))

    subject, text = _render_email("stripe_review_status_changed", context)
    return send_email(subject=subject, text=text, to=to_email)
