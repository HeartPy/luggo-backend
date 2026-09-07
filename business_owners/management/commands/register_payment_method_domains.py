"""
既存事業者のサブドメインを Stripe Payment Method Domain へ一括登録するコマンド

Apple Pay をサブドメイン付き予約サイトで使えるようにするための処理。
新規登録時は Celery タスクで自動登録されるが、登録に失敗したドメインの
再実行にこのコマンドを使う。

使い方:
    python manage.py register_payment_method_domains
    python manage.py register_payment_method_domains --subdomain xxx
"""
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.core.management.base import BaseCommand

from business_owners.models import BusinessProfile
from business_owners.payment_method_domains import (
    build_tenant_domain,
    ensure_payment_method_domain,
)


class Command(BaseCommand):
    help = '事業者サブドメインを Stripe Payment Method Domain（Apple Pay 用）へ一括登録する'

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            '--subdomain',
            type=str,
            default=None,
            help='指定したサブドメインのみ登録する（省略時は有効な全事業者＋ルートドメイン）',
        )

    def handle(self, *args: Any, **options: Any) -> None:
        subdomain_filter = options['subdomain']

        domains: list[str] = []

        if subdomain_filter:
            domain = build_tenant_domain(subdomain_filter)
            if domain is None:
                self.stdout.write(self.style.WARNING(
                    '開発環境（localhost）のため登録をスキップしました'
                ))
                return
            domains.append(domain)
        else:
            # ルートドメイン（例: luggo.delivery）も登録対象に含める
            root_domain = self._build_root_domain()
            if root_domain is None:
                self.stdout.write(self.style.WARNING(
                    '開発環境（localhost）のため登録をスキップしました'
                ))
                return
            domains.append(root_domain)

            subdomains = (
                BusinessProfile.objects
                .filter(is_active=True)
                .exclude(subdomain='')
                .values_list('subdomain', flat=True)
                .order_by('subdomain')
            )
            for subdomain in subdomains:
                domain = build_tenant_domain(subdomain)
                if domain is not None:
                    domains.append(domain)

        success_count = 0
        failure_count = 0
        for domain in domains:
            try:
                ensure_payment_method_domain(domain)
                success_count += 1
                self.stdout.write(f'登録OK: {domain}')
            except Exception as e:
                failure_count += 1
                self.stderr.write(self.style.ERROR(
                    f'登録失敗: {domain} error={e}'
                ))

        self.stdout.write(self.style.SUCCESS(
            f'完了: 成功 {success_count} 件 / 失敗 {failure_count} 件'
        ))

    @staticmethod
    def _build_root_domain() -> str | None:
        """FRONTEND_BASE_URL からルートドメイン（サブドメインなし）を導出"""
        hostname = urlsplit(settings.FRONTEND_BASE_URL).hostname or ''
        if not hostname or hostname in ('localhost', '127.0.0.1'):
            return None
        return hostname
