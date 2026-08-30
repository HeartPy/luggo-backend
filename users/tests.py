"""事業者の無効化がログインとダッシュボード API を止めることのテスト"""
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from business_owners.models import BusinessProfile
from drivers.models import DriverProfile
from users.models import User
from users.utils import generate_login_verification_code


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


class BusinessOwnerLoginFlowTests(TestCase):
    """事業者ログイン（認証コード → セッション確立）のテスト"""

    def setUp(self) -> None:
        self.password = 'OwnerPass123!'
        self.owner_user = User.objects.create_user(
            email='login-owner@example.com',
            password=self.password,
            user_type='business_owner',
        )
        self.business_profile = BusinessProfile.objects.create(
            user=self.owner_user,
            company_name='ログインテスト運送',
            company_email='login-owner@example.com',
            subdomain='loginowner',
        )
        self.client = APIClient()
        # IP ベースのログイン試行制限が他テストの失敗記録に影響されないようにする
        cache.clear()

    def _send_code(self, email='login-owner@example.com', password=None):
        with patch(
            'users.views.send_login_verification_code', return_value=True
        ) as send_code:
            response = self.client.post(
                reverse('send_login_code'),
                {'email': email, 'password': password or self.password},
                format='json',
            )
        return response, send_code

    def test_send_login_code_succeeds_with_valid_credentials(self) -> None:
        # Act: 正しいメールアドレスとパスワードで認証コード送信を要求する
        response, send_code = self._send_code()

        # Assert: メールが送られ、6桁の認証コードが保存される
        self.assertEqual(response.status_code, 200)
        send_code.assert_called_once()
        self.owner_user.refresh_from_db()
        self.assertRegex(self.owner_user.verification_code, r'^\d{6}$')
        self.assertIsNotNone(self.owner_user.verification_code_expires_at)

    def test_send_login_code_rejects_wrong_password(self) -> None:
        # Act: 誤ったパスワードで認証コード送信を要求する
        response, send_code = self._send_code(password='WrongPass123!')

        # Assert: 401 になりメールは送られない
        self.assertEqual(response.status_code, 401)
        send_code.assert_not_called()

    def test_send_login_code_rejects_non_owner_user(self) -> None:
        # Arrange: 配達者アカウントを用意する
        User.objects.create_user(
            email='driver-as-owner@example.com',
            password=self.password,
            user_type='delivery_driver',
        )

        # Act: 配達者の認証情報で事業者ログインを試す
        response, send_code = self._send_code(email='driver-as-owner@example.com')

        # Assert: 事業者以外は拒否されメールは送られない
        self.assertEqual(response.status_code, 401)
        send_code.assert_not_called()

    def test_verify_login_code_creates_session(self) -> None:
        # Arrange: 認証コードを発行する
        code = generate_login_verification_code(self.owner_user)

        # Act: 認証コードを検証する
        response = self.client.post(
            reverse('verify_login_code'),
            {'email': 'login-owner@example.com', 'code': code},
            format='json',
        )

        # Assert: ログインに成功し、セッションで認証済みになる
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['user_type'], 'business_owner')
        self.assertIn('dashboard_url', response.data)
        auth_response = self.client.get(reverse('check_authentication'))
        self.assertEqual(auth_response.status_code, 200)
        self.assertTrue(auth_response.data['authenticated'])

        # Assert: 使用済みの認証コードはクリアされる
        self.owner_user.refresh_from_db()
        self.assertEqual(self.owner_user.verification_code, '')

    def test_verify_login_code_rejects_wrong_code(self) -> None:
        # Arrange: 認証コードを発行する
        code = generate_login_verification_code(self.owner_user)
        wrong_code = '000000' if code != '000000' else '111111'

        # Act: 誤った認証コードを送る
        response = self.client.post(
            reverse('verify_login_code'),
            {'email': 'login-owner@example.com', 'code': wrong_code},
            format='json',
        )

        # Assert: 401 になり失敗回数が記録される
        self.assertEqual(response.status_code, 401)
        self.owner_user.refresh_from_db()
        self.assertEqual(self.owner_user.verification_code_attempts, 1)

    def test_verify_login_code_locks_after_three_failures(self) -> None:
        # Arrange: 認証コードを発行する
        code = generate_login_verification_code(self.owner_user)
        wrong_code = '000000' if code != '000000' else '111111'
        url = reverse('verify_login_code')
        payload = {'email': 'login-owner@example.com', 'code': wrong_code}

        # Act: 3回連続で誤ったコードを送る
        self.client.post(url, payload, format='json')
        self.client.post(url, payload, format='json')
        third_response = self.client.post(url, payload, format='json')

        # Assert: 3回目で認証コードが無効化され、再送信が必要になる
        self.assertEqual(third_response.status_code, 400)
        self.owner_user.refresh_from_db()
        self.assertEqual(self.owner_user.verification_code, '')

        # Assert: 無効化後は正しかったはずのコードでもログインできない
        retry_response = self.client.post(
            url,
            {'email': 'login-owner@example.com', 'code': code},
            format='json',
        )
        self.assertEqual(retry_response.status_code, 401)

    def test_verify_login_code_rejects_expired_code(self) -> None:
        # Arrange: 認証コードを発行し、有効期限を過去にする
        code = generate_login_verification_code(self.owner_user)
        self.owner_user.verification_code_expires_at = (
            timezone.now() - timezone.timedelta(minutes=1)
        )
        self.owner_user.save(update_fields=['verification_code_expires_at'])

        # Act: 期限切れの認証コードを送る
        response = self.client.post(
            reverse('verify_login_code'),
            {'email': 'login-owner@example.com', 'code': code},
            format='json',
        )

        # Assert: 401 になりログインできない
        self.assertEqual(response.status_code, 401)
        auth_response = self.client.get(reverse('check_authentication'))
        self.assertNotEqual(auth_response.status_code, 200)


class DriverLoginTests(TestCase):
    """配達者ログイン（パスワードのみ・二段階認証なし）のテスト"""

    def setUp(self) -> None:
        self.password = 'DriverPass123!'
        owner_user = User.objects.create_user(
            email='owner-for-driver-login@example.com',
            password='OwnerPass123!',
            user_type='business_owner',
        )
        self.business_profile = BusinessProfile.objects.create(
            user=owner_user,
            company_name='配達者ログインテスト運送',
            company_email='owner-for-driver-login@example.com',
            subdomain='drvlogin',
        )
        self.driver_user = User.objects.create_user(
            email='login-driver@example.com',
            password=self.password,
            user_type='delivery_driver',
            last_name='配達',
            first_name='太郎',
        )
        self.driver = DriverProfile.objects.create(
            user=self.driver_user,
            business_owner=self.business_profile,
            license_expiry=timezone.localdate() + timedelta(days=365),
        )
        self.client = APIClient()
        cache.clear()

    def test_driver_login_succeeds_and_creates_session(self) -> None:
        # Act: 正しいメールアドレスとパスワードでログインする
        response = self.client.post(
            reverse('driver_login'),
            {'email': 'login-driver@example.com', 'password': self.password},
            format='json',
        )

        # Assert: ログインに成功し、セッションで担当予約 API を呼べる
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['user_type'], 'delivery_driver')
        bookings_response = self.client.get(
            reverse('driver_my_bookings'),
            {'date': timezone.localdate().isoformat()},
        )
        self.assertEqual(bookings_response.status_code, 200)

    def test_driver_login_rejects_wrong_password(self) -> None:
        # Act: 誤ったパスワードでログインを試す
        response = self.client.post(
            reverse('driver_login'),
            {'email': 'login-driver@example.com', 'password': 'WrongPass123!'},
            format='json',
        )

        # Assert: 401 になりセッションは作られない
        self.assertEqual(response.status_code, 401)
        bookings_response = self.client.get(
            reverse('driver_my_bookings'),
            {'date': timezone.localdate().isoformat()},
        )
        self.assertNotEqual(bookings_response.status_code, 200)


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
            license_expiry=timezone.localdate() + timedelta(days=365),
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
