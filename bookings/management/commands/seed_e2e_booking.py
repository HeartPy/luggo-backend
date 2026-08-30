"""
E2E テスト用のシードデータを作成する管理コマンド

冪等に動作する（何度実行しても同じ事業者・配達者・予約が同じ状態になる）。
Playwright の globalSetup から毎回実行される前提。

作成するもの:
- 事業者（subdomain: etoe）+ ログイン用パスワード
- 配達者（etoe 事業者に所属）+ ログイン用パスワード
- 事業者の予約管理用の予約 1 件（集荷前・担当なし）
- 配達者フロー用の予約 1 件（集荷前・配達者に割当・当日日付）
"""

from datetime import timedelta
from typing import Any

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from bookings.models import LuggageBooking
from business_owners.models import BusinessProfile
from business_owners.policy_versions import (
    BOOKING_PRIVACY_VERSION,
    BOOKING_TRANSACTION_LAW_VERSION,
    PRIVACY_VERSION,
    TERMS_VERSION,
)
from drivers.models import DriverProfile


# サブドメインは 3〜12 文字の半角小文字英字のみ（"e2e" は数字を含むため使えない）
E2E_SUBDOMAIN = 'etoe'
E2E_OWNER_EMAIL = 'e2e-owner@example.com'
E2E_OWNER_PASSWORD = 'e2e-owner-password'
E2E_DRIVER_EMAIL = 'e2e-driver@example.com'
E2E_DRIVER_PASSWORD = 'e2e-driver-password'

# E2E シード予約の識別マーカー（customer_email。再シード時に削除して作り直す）
E2E_OWNER_BOOKING_CUSTOMER_EMAIL = 'e2e-owner-booking@example.com'
E2E_DRIVER_DELIVERY_CUSTOMER_EMAIL = 'e2e-driver-delivery@example.com'

SEED_ONLY_OWNER_BOOKING = 'owner-booking'
SEED_ONLY_DRIVER_DELIVERY = 'driver-delivery'
SEED_ONLY_CHOICES = [SEED_ONLY_OWNER_BOOKING, SEED_ONLY_DRIVER_DELIVERY]

# 東京都（郵便番号 100xxxx / 160xxxx が対象になる）
TOKYO_PREF_CODE = '13'

# 予約の共通フィールド（テストヘルパー bookings/tests/helpers.py と同等）
BOOKING_COMMON = {
    'pickup_location_name': '東京駅',
    'pickup_location_address': '東京都千代田区丸の内1-1-1',
    'delivery_location_name': '新宿駅',
    'delivery_location_address': '東京都新宿区新宿3-38-1',
    'customer_phone_number': '09012345678',
    'customer_nationality': 'JPN',
    'luggage_items': {'cabin': 1, 'checked': 0, 'oversize': 0},
    'total_amount': 1000,
}


class Command(BaseCommand):
    help = 'E2E テスト用の事業者・配達者・予約データを冪等に作成する'

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            '--only',
            choices=SEED_ONLY_CHOICES,
            help=(
                '指定したテスト用の予約だけを再シードする。'
                'テストが並列実行されても互いの予約を消さないようにするため、'
                '各スペックの beforeEach からは自分の分だけを指定して呼ぶ。'
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        profile = self._seed_owner()
        driver = self._seed_driver(profile)
        self._seed_bookings(profile, driver, options.get('only'))

        self.stdout.write(
            self.style.SUCCESS(
                'E2E シード完了: subdomain=%s business_profile_id=%s driver_id=%s'
                % (E2E_SUBDOMAIN, profile.id, driver.id)
            )
        )

    def _seed_owner(self) -> BusinessProfile:
        """E2E 用事業者（owner-booking E2E でログインするためパスワードも設定する）"""
        user_model = get_user_model()
        user, _ = user_model.objects.get_or_create(
            email=E2E_OWNER_EMAIL,
            defaults={
                'user_type': 'business_owner',
                'is_active': True,
            },
        )
        # 冪等にパスワードを固定する（owner-booking E2E のログインで使用）
        user.set_password(E2E_OWNER_PASSWORD)
        user.save(update_fields=['password'])

        now = timezone.now()
        profile, _ = BusinessProfile.objects.update_or_create(
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
                # 規約改訂・テンプレート更新・公開情報同意のダイアログが
                # ダッシュボード操作を妨げないよう、すべて同意済みにする
                'public_info_consent_at': now,
                'terms_agreed_version': TERMS_VERSION,
                'terms_agreed_at': now,
                'privacy_agreed_version': PRIVACY_VERSION,
                'privacy_agreed_at': now,
                'booking_transaction_law_acknowledged_version':
                    BOOKING_TRANSACTION_LAW_VERSION,
                'booking_transaction_law_acknowledged_at': now,
                'booking_privacy_acknowledged_version': BOOKING_PRIVACY_VERSION,
                'booking_privacy_acknowledged_at': now,
            },
        )
        return profile

    def _seed_driver(self, profile: BusinessProfile) -> DriverProfile:
        """E2E 用配達者（招待フローは結合テストで担保済みのため直接作成する）"""
        user_model = get_user_model()
        user, _ = user_model.objects.get_or_create(
            email=E2E_DRIVER_EMAIL,
            defaults={
                'user_type': 'delivery_driver',
                'is_active': True,
            },
        )
        user.set_password(E2E_DRIVER_PASSWORD)
        user.save(update_fields=['password'])

        driver, _ = DriverProfile.objects.update_or_create(
            user=user,
            defaults={
                'business_owner': profile,
                'company_name': 'E2E テスト配達者',
                'license_expiry': timezone.localdate() + timedelta(days=365),
                'is_active': True,
            },
        )
        return driver

    def _seed_bookings(
        self,
        profile: BusinessProfile,
        driver: DriverProfile,
        only: str | None = None,
    ) -> None:
        """
        owner-booking / driver-delivery 用の予約を削除 → 再作成で冪等にシードする

        only を指定した場合はその予約だけを対象にする（並列実行中の
        もう一方のテストの予約を消さないため）。
        """
        target_emails = {
            SEED_ONLY_OWNER_BOOKING: [E2E_OWNER_BOOKING_CUSTOMER_EMAIL],
            SEED_ONLY_DRIVER_DELIVERY: [E2E_DRIVER_DELIVERY_CUSTOMER_EMAIL],
        }.get(
            only or '',
            [E2E_OWNER_BOOKING_CUSTOMER_EMAIL, E2E_DRIVER_DELIVERY_CUSTOMER_EMAIL],
        )

        LuggageBooking.objects.filter(
            business_owner=profile,
            customer_email__in=target_emails,
        ).delete()

        today = timezone.localdate()

        # owner-booking: 事業者が一覧でステータスを更新する予約（当日日付で一覧上位に出す）
        if E2E_OWNER_BOOKING_CUSTOMER_EMAIL in target_emails:
            LuggageBooking.objects.create(
                business_owner=profile,
                customer_name='E2E Owner Booking',
                guest_name='E2E Owner Booking',
                customer_email=E2E_OWNER_BOOKING_CUSTOMER_EMAIL,
                pickup_date=today,
                delivery_date=today,
                delivery_status='before_pickup',
                notes='E2E owner-booking 用シード予約',
                payment_intent_id='pi_e2e_seed_owner_booking',
                **BOOKING_COMMON,
            )

        # driver-delivery: 集荷前の担当予約（当日表示。E2E で集荷完了→配達完了まで操作する）
        if E2E_DRIVER_DELIVERY_CUSTOMER_EMAIL in target_emails:
            LuggageBooking.objects.create(
                business_owner=profile,
                customer_name='E2E Driver Delivery',
                guest_name='E2E Driver Delivery',
                customer_email=E2E_DRIVER_DELIVERY_CUSTOMER_EMAIL,
                pickup_date=today,
                delivery_date=today,
                delivery_status='before_pickup',
                driver=driver,
                delivery_manually_assigned=True,
                notes='E2E driver-delivery 用シード予約',
                payment_intent_id='pi_e2e_seed_driver_delivery',
                **BOOKING_COMMON,
            )
