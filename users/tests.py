"""事業者の無効化がログインとダッシュボード API を止めることのテスト"""
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from business_owners.models import BusinessProfile
from drivers.models import DriverProfile
from users.models import User


class InactiveBusinessOwnerAccessTests(TestCase):
    """事業者を無効にするとログインもダッシュボード API も使えない"""

    def setUp(self) -> None:
        self.password = 'owner-test-pass-123'
        self.owner_user = User.objects.create_user(
            email='owner@example.com',
            password=self.password,
            user_type='business_owner',
        )
        self.business_profile = BusinessProfile.objects.create(
            user=self.owner_user,
            company_name='テスト運送',
            company_email='owner@example.com',
            subdomain='sampleco',
        )
        self.client = APIClient()

    def test_send_login_code_is_rejected_when_business_is_inactive(self) -> None:
        # Arrange: 事業者を無効化する
        self.business_profile.is_active = False
        self.business_profile.save()

        # Act: 正しいパスワードでログインコード送信を試す
        with patch('users.views.send_login_verification_code') as send_code:
            response = self.client.post(
                reverse('send_login_code'),
                {'email': 'owner@example.com', 'password': self.password},
                format='json',
            )

        # Assert: 認証コードは送られず、ログイン失敗と同じ応答になる
        self.assertEqual(response.status_code, 401)
        send_code.assert_not_called()

    def test_auth_check_and_profile_api_are_rejected_after_deactivation(
        self,
    ) -> None:
        # Arrange: ログイン済みの状態から事業者を無効化する
        self.client.force_login(self.owner_user)
        self.business_profile.is_active = False
        self.business_profile.save()

        # Act: 認証確認とダッシュボード用プロフィール API を呼ぶ
        auth_response = self.client.get(reverse('check_authentication'))
        profile_response = self.client.get(
            reverse('get_current_business_profile')
        )

        # Assert: どちらも未認証として拒否される
        self.assertEqual(auth_response.status_code, 403)
        self.assertEqual(profile_response.status_code, 403)

    def test_bookings_api_is_rejected_when_profile_is_inactive_without_user_sync(
        self,
    ) -> None:
        # Arrange: User は有効のまま、事業者だけを無効にする
        self.client.force_authenticate(user=self.owner_user)
        BusinessProfile.objects.filter(pk=self.business_profile.pk).update(
            is_active=False,
        )
        self.owner_user.refresh_from_db()
        self.assertTrue(self.owner_user.is_active)

        # Act: 予約一覧 API を呼ぶ
        response = self.client.get(reverse('business_bookings_list'))

        # Assert: 有効な事業者として扱われない
        self.assertEqual(response.status_code, 404)


class InactiveDriverAccessTests(TestCase):
    """配達者を無効にするとログインもダッシュボード API も使えない"""

    def setUp(self) -> None:
        self.password = 'driver-test-pass-123'
        self.owner_user = User.objects.create_user(
            email='owner-for-driver@example.com',
            password='owner-test-pass-123',
            user_type='business_owner',
        )
        self.business_profile = BusinessProfile.objects.create(
            user=self.owner_user,
            company_name='テスト運送',
            company_email='owner-for-driver@example.com',
            subdomain='drvowner',
        )
        self.driver_user = User.objects.create_user(
            email='driver@example.com',
            password=self.password,
            user_type='delivery_driver',
            first_name='太郎',
            last_name='配達',
        )
        self.driver = DriverProfile.objects.create(
            user=self.driver_user,
            business_owner=self.business_profile,
            license_expiry=timezone.localdate(),
        )
        self.client = APIClient()

    def test_login_is_rejected_when_driver_is_inactive(self) -> None:
        # Arrange: 配達者を無効化する
        self.driver.is_active = False
        self.driver.save()

        # Act: 正しいパスワードでログインを試す
        response = self.client.post(
            reverse('driver_login'),
            {'email': 'driver@example.com', 'password': self.password},
            format='json',
        )

        # Assert: ログイン失敗と同じ応答になる
        self.assertEqual(response.status_code, 401)

    def test_auth_check_and_me_api_are_rejected_after_deactivation(self) -> None:
        # Arrange: ログイン済みの状態から配達者を無効化する
        self.client.force_login(self.driver_user)
        self.driver.is_active = False
        self.driver.save()

        # Act: 認証確認と配達者ダッシュボード API を呼ぶ
        auth_response = self.client.get(reverse('check_authentication'))
        bookings_response = self.client.get(
            reverse('driver_my_bookings'),
            {'date': timezone.localdate().isoformat()},
        )

        # Assert: どちらも拒否される
        self.assertEqual(auth_response.status_code, 403)
        self.assertEqual(bookings_response.status_code, 403)

    def test_login_is_rejected_when_business_is_inactive(self) -> None:
        # Arrange: 所属事業者を無効化する（配達者も連動して停止する）
        self.business_profile.is_active = False
        self.business_profile.save()
        self.driver.refresh_from_db()
        self.assertFalse(self.driver.is_active)

        # Act: 正しいパスワードでログインを試す
        response = self.client.post(
            reverse('driver_login'),
            {'email': 'driver@example.com', 'password': self.password},
            format='json',
        )

        # Assert: ログインできない
        self.assertEqual(response.status_code, 401)
