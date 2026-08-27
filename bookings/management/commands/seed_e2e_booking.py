"""
E2E テスト（旅行者予約）用のシードデータを作成する管理コマンド

冪等に動作する（何度実行しても同じ事業者が同じ設定になる）。
Playwright の globalSetup から毎回実行される前提。
"""

from typing import Any

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from business_owners.models import BusinessProfile


# サブドメインは 3〜12 文字の半角小文字英字のみ（"e2e" は数字を含むため使えない）
E2E_SUBDOMAIN = 'etoe'
E2E_OWNER_EMAIL = 'e2e-owner@example.com'

# 東京都（郵便番号 100xxxx / 160xxxx が対象になる）
TOKYO_PREF_CODE = '13'


class Command(BaseCommand):
    help = 'E2E テスト用の事業者データを冪等に作成する'

    def handle(self, *args: Any, **options: Any) -> None:
        user_model = get_user_model()
        user, user_created = user_model.objects.get_or_create(
            email=E2E_OWNER_EMAIL,
            defaults={
                'user_type': 'business_owner',
                'is_active': True,
            },
        )
        if user_created:
            user.set_unusable_password()
            user.save(update_fields=['password'])

        profile, profile_created = BusinessProfile.objects.update_or_create(
            subdomain=E2E_SUBDOMAIN,
            defaults={
                'user': user,
                'company_name': 'E2E テスト事業者',
                'company_email': E2E_OWNER_EMAIL,
                'service_areas': [TOKYO_PREF_CODE],
                'pricing_rules': {
                    TOKYO_PREF_CODE: {'cabin': 1000, 'checked': 2000},
                },
                'operating_days': '1111111',
                'nth_weekday_holidays': [],
                'temporary_closures': [],
                'daily_max_luggage': -1,
                'is_active': True,
                # E2E は Stripe をモックするため、実在しないダミー ID でよい
                'stripe_account_id': 'acct_e2e_mock',
            },
        )

        self.stdout.write(
            self.style.SUCCESS(
                'E2E シード完了: subdomain=%s business_profile_id=%s (%s)'
                % (
                    E2E_SUBDOMAIN,
                    profile.id,
                    '新規作成' if profile_created else '更新',
                )
            )
        )
