"""事業者関連の Celery ジョブ"""
import logging

import stripe
from celery import shared_task

from .payment_method_domains import build_tenant_domain, ensure_payment_method_domain

logger = logging.getLogger(__name__)


@shared_task(
    autoretry_for=(stripe.StripeError,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
)
def register_payment_method_domain(subdomain: str) -> None:
    """
    サブドメイン確定時に Stripe Payment Method Domain を自動登録

    Apple Pay をサブドメイン付き予約サイトで使えるようにするための処理。
    Stripe API エラー時は指数バックオフで最大 5 回リトライする。
    """
    domain_name = build_tenant_domain(subdomain)
    if domain_name is None:
        logger.info(
            "開発環境のため Payment Method Domain 登録をスキップ: subdomain=%s",
            subdomain,
        )
        return

    ensure_payment_method_domain(domain_name)
