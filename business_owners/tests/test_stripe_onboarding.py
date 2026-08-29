"""
事業者の Stripe オンボーディング（連結アカウント API）のテスト

Stripe Connect の API はすべてモックする（E2E 用 stripe_mock は PaymentIntent
専用のためここでは使わない）。
"""
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from users.models import User
from business_owners.models import BusinessProfile


class _StripeObject(dict):
    """属性アクセスできる dict（Stripe オブジェクトの代替・JSONレンダリング可能）"""

    __getattr__ = dict.get


class StripeOnboardingAPITest(TestCase):
    """連結アカウントの作成・取得APIのテスト"""

    def setUp(self) -> None:
        self.owner_user = User.objects.create_user(
            email='onboarding-owner@example.com',
            password='OwnerPass123!',
            user_type='business_owner',
        )
        self.profile = BusinessProfile.objects.create(
            user=self.owner_user,
            company_name='オンボーディングテスト運送',
            company_email='onboarding-owner@example.com',
            subdomain='onboardone',
        )
        self.client = APIClient()

    def _login(self):
        self.client.force_authenticate(self.owner_user)

    @patch('business_owners.views.stripe.Account.modify')
    @patch('business_owners.views.stripe.Account.create')
    def test_create_account_saves_stripe_account_id(
        self, create_mock, modify_mock
    ) -> None:
        # Arrange: 公開情報の表示に同意済みの事業者と、Stripe アカウント作成をモック
        self.profile.public_info_consent_at = timezone.now()
        self.profile.save(update_fields=['public_info_consent_at'])
        create_mock.return_value = _StripeObject(id='acct_new_1')
        self._login()

        # Act: 連結アカウント作成APIを呼び出す
        response = self.client.post(
            reverse('stripe_custom_create_account'),
            {'business_type': 'individual'},
            format='json',
        )

        # Assert: 201 で作成され、stripe_account_id が保存される
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['account_id'], 'acct_new_1')
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.stripe_account_id, 'acct_new_1')

        # Assert: custom アカウントとして日本向けに作成される
        _, create_kwargs = create_mock.call_args
        self.assertEqual(create_kwargs['type'], 'custom')
        self.assertEqual(create_kwargs['country'], 'JP')
        self.assertEqual(create_kwargs['business_type'], 'individual')

    @patch('business_owners.views.stripe.Account.create')
    def test_create_account_requires_public_info_consent(
        self, create_mock
    ) -> None:
        # Arrange: 同意未取得のままログインする
        self._login()

        # Act: 連結アカウント作成APIを呼び出す
        response = self.client.post(
            reverse('stripe_custom_create_account'),
            {'business_type': 'individual'},
            format='json',
        )

        # Assert: 403 になり Stripe を呼ばない
        self.assertEqual(response.status_code, 403)
        create_mock.assert_not_called()
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.stripe_account_id)

    @patch('business_owners.views.stripe.Account.create')
    def test_create_account_rejects_duplicate(self, create_mock) -> None:
        # Arrange: 既に連結アカウントを持つ事業者を用意する
        self.profile.public_info_consent_at = timezone.now()
        self.profile.stripe_account_id = 'acct_existing'
        self.profile.save(
            update_fields=['public_info_consent_at', 'stripe_account_id']
        )
        self._login()

        # Act: 連結アカウント作成APIを再度呼び出す
        response = self.client.post(
            reverse('stripe_custom_create_account'),
            {'business_type': 'individual'},
            format='json',
        )

        # Assert: 400 になり Stripe を呼ばない
        self.assertEqual(response.status_code, 400)
        create_mock.assert_not_called()

    def test_create_account_requires_authentication(self) -> None:
        # Act: 未認証で連結アカウント作成APIを呼び出す
        response = self.client.post(
            reverse('stripe_custom_create_account'),
            {'business_type': 'individual'},
            format='json',
        )

        # Assert: 認証エラーになる
        self.assertIn(response.status_code, (401, 403))

    @patch('business_owners.views.stripe.Account.retrieve')
    def test_get_account_returns_own_account(self, retrieve_mock) -> None:
        # Arrange: 連結アカウント作成済みの事業者と Stripe 取得をモック
        self.profile.stripe_account_id = 'acct_existing'
        self.profile.save(update_fields=['stripe_account_id'])
        retrieve_mock.return_value = _StripeObject(
            id='acct_existing',
            business_type='individual',
        )
        self._login()

        # Act: 連結アカウント取得APIを呼び出す
        response = self.client.get(reverse('stripe_custom_get_account'))

        # Assert: 自社のアカウント情報が返る
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['account_id'], 'acct_existing')
        retrieve_mock.assert_called_once_with('acct_existing')

    def test_get_account_returns_404_before_onboarding(self) -> None:
        # Arrange: 連結アカウント未作成のままログインする
        self._login()

        # Act: 連結アカウント取得APIを呼び出す
        response = self.client.get(reverse('stripe_custom_get_account'))

        # Assert: 404 になる
        self.assertEqual(response.status_code, 404)
