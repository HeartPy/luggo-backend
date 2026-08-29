"""事業者アカウント登録（メールアドレス確認 → トークン → 登録）のテスト"""
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from users.models import User
from business_owners.models import BusinessProfile, RegistrationToken
from business_owners.utils import generate_registration_token


class CheckEmailAvailabilityTests(TestCase):
    """登録前のメールアドレス重複チェックAPIのテスト"""

    def setUp(self) -> None:
        self.client = APIClient()
        self.url = reverse('check_email_availability')

    def test_available_email(self) -> None:
        # Act: 未登録のメールアドレスをチェックする
        response = self.client.get(self.url, {'email': 'new-owner@example.com'})

        # Assert: 使用可能と返る
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['available'])

    def test_duplicate_email(self) -> None:
        # Arrange: 既存ユーザーを作成する
        User.objects.create_user(
            email='taken-owner@example.com',
            password='OwnerPass123!',
            user_type='business_owner',
        )

        # Act: 登録済みのメールアドレスをチェックする
        response = self.client.get(self.url, {'email': 'taken-owner@example.com'})

        # Assert: 使用不可と返る
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['available'])

    def test_missing_email(self) -> None:
        # Act: メールアドレスなしでチェックする
        response = self.client.get(self.url)

        # Assert: 400 になる
        self.assertEqual(response.status_code, 400)


class BusinessRegistrationFlowTests(TestCase):
    """登録メール → トークン検証 → アカウント登録のフローのテスト"""

    EMAIL = 'register-owner@example.com'

    def setUp(self) -> None:
        self.client = APIClient()

    def _register_payload(self, token: str, **overrides):
        payload = {
            'token': token,
            'business_type': 'individual',
            'company_name': '登録テスト運送',
            'rep_last_name': '山田',
            'rep_first_name': '太郎',
            'rep_last_name_kana': 'ヤマダ',
            'rep_first_name_kana': 'タロウ',
            'email': self.EMAIL,
            'phone': '0312345678',
            'subdomain': 'regowner',
            'password': 'StrongPass123!',
        }
        payload.update(overrides)
        return payload

    def test_full_registration_flow(self) -> None:
        # Act: 登録用メールの送信をリクエストする（メール送信はモック）
        with patch(
            'business_owners.views.send_registration_email', return_value=True
        ) as send_mail:
            request_response = self.client.post(
                reverse('request_registration_email'),
                {'email': self.EMAIL},
                format='json',
            )

        # Assert: メールが送られ、トークンが発行される
        self.assertEqual(request_response.status_code, 200)
        send_mail.assert_called_once()
        token = RegistrationToken.objects.get(email=self.EMAIL)

        # Act: メール内リンクのトークンを検証する
        verify_response = self.client.get(
            reverse('verify_registration_token'), {'token': token.token}
        )

        # Assert: 有効なトークンとしてメールアドレスが返る
        self.assertEqual(verify_response.status_code, 200)
        self.assertTrue(verify_response.data['valid'])
        self.assertEqual(verify_response.data['email'], self.EMAIL)

        # Act: アカウント登録を実行する（完了通知メールはモック）
        with patch('business_owners.views.send_registration_completed_emails'):
            register_response = self.client.post(
                reverse('register_business_account'),
                self._register_payload(token.token),
                format='json',
            )

        # Assert: 事業者ユーザーとプロフィールが作成される
        self.assertEqual(register_response.status_code, 201)
        user = User.objects.get(email=self.EMAIL)
        self.assertEqual(user.user_type, 'business_owner')
        self.assertTrue(user.check_password('StrongPass123!'))
        profile = BusinessProfile.objects.get(user=user)
        self.assertEqual(profile.subdomain, 'regowner')
        self.assertIsNone(profile.stripe_account_id)

        # Assert: トークンは使用済みになり再利用できない
        token.refresh_from_db()
        self.assertIsNotNone(token.used_at)

    def test_register_rejects_invalid_token(self) -> None:
        # Act: 存在しないトークンで登録を試みる
        response = self.client.post(
            reverse('register_business_account'),
            self._register_payload('invalid-token'),
            format='json',
        )

        # Assert: 400 になりユーザーは作成されない
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(email=self.EMAIL).exists())

    def test_register_rejects_expired_token(self) -> None:
        # Arrange: 期限切れのトークンを用意する
        token = generate_registration_token(self.EMAIL)
        RegistrationToken.objects.filter(pk=token.pk).update(
            expires_at=timezone.now() - timezone.timedelta(minutes=1)
        )

        # Act: 期限切れトークンで登録を試みる
        response = self.client.post(
            reverse('register_business_account'),
            self._register_payload(token.token),
            format='json',
        )

        # Assert: 400 になりユーザーは作成されない
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(email=self.EMAIL).exists())

    def test_register_rejects_email_mismatch(self) -> None:
        # Arrange: 別のメールアドレス宛のトークンを用意する
        token = generate_registration_token('someone-else@example.com')

        # Act: トークンの宛先と異なるメールアドレスで登録を試みる
        response = self.client.post(
            reverse('register_business_account'),
            self._register_payload(token.token),
            format='json',
        )

        # Assert: 400 になりユーザーは作成されない
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(email=self.EMAIL).exists())

    def test_register_rejects_weak_password(self) -> None:
        # Arrange: 有効なトークンと弱いパスワードを用意する
        token = generate_registration_token(self.EMAIL)

        # Act: 文字種が足りないパスワードで登録を試みる
        response = self.client.post(
            reverse('register_business_account'),
            self._register_payload(token.token, password='alllowercase'),
            format='json',
        )

        # Assert: 400 になりユーザーは作成されない
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(email=self.EMAIL).exists())
