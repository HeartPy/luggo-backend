"""
Stripe Payment Method Domain（Apple Pay 等のドメイン登録）関連のヘルパー

サブドメイン付きの予約サイトで Apple Pay を使うには、各ドメインを
Stripe の Payment Method Domains API に登録して検証を通す必要がある。
決済はプラットフォーム（親）アカウントで行っているため、
プラットフォームの秘密鍵で登録すれば全テナントに有効になる。
"""
import logging
from urllib.parse import urlsplit

import stripe
from django.conf import settings

from project.utils import stripe_get

logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY


def build_tenant_domain(subdomain: str) -> str | None:
    """
    FRONTEND_BASE_URL からテナントの公開ドメイン（例: xxx.luggo.delivery）を導出

    開発環境（localhost / 127.0.0.1）ではサブドメイン配信をしていないため
    None を返し、呼び出し側で登録をスキップさせる。
    """
    parts = urlsplit(settings.FRONTEND_BASE_URL)
    hostname = parts.hostname or ""

    if not hostname or hostname in ("localhost", "127.0.0.1"):
        return None

    # build_booking_form_url と同じロジックでベースドメインを導出
    host_parts = hostname.split(".")
    base_domain = ".".join(host_parts[1:]) if len(host_parts) >= 3 else hostname
    return f"{subdomain}.{base_domain}"


def ensure_payment_method_domain(domain_name: str) -> None:
    """
    Payment Method Domain を冪等に登録・検証

    - 既に登録済みなら create はスキップ
    - Apple Pay が inactive のままなら validate を実行して再検証を試みる

    Stripe API エラーはそのまま送出する（リトライは呼び出し側で制御）。
    """
    existing = stripe.PaymentMethodDomain.list(domain_name=domain_name, limit=1)
    domains = stripe_get(existing, "data") or []

    if domains:
        domain = domains[0]
        logger.info(
            "Payment Method Domain は登録済み: domain=%s", domain_name
        )
    else:
        domain = stripe.PaymentMethodDomain.create(domain_name=domain_name)
        logger.info(
            "Payment Method Domain を登録しました: domain=%s", domain_name
        )

    apple_pay = stripe_get(domain, "apple_pay")
    apple_pay_status = stripe_get(apple_pay, "status")
    if apple_pay_status != "active":
        domain_id = stripe_get(domain, "id")
        stripe.PaymentMethodDomain.validate(domain_id)
        logger.info(
            "Payment Method Domain の検証を実行しました: domain=%s (apple_pay=%s)",
            domain_name,
            apple_pay_status,
        )
