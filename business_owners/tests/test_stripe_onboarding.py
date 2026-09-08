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

    @patch('business_owners.views.stripe.Account.create')
    def test_create_account_rejects_empty_payload(
        self, create_mock
    ) -> None:
        # Arrange: 同意済みの事業者が、フォームなし（business_type のみ）で作成しようとする
        self.profile.public_info_consent_at = timezone.now()
        self.profile.save(update_fields=['public_info_consent_at'])
        self._login()

        # Act: フォームデータなしで連結アカウント作成APIを呼び出す
        response = self.client.post(
            reverse('stripe_custom_create_account'),
            {'business_type': 'individual'},
            format='json',
        )

        # Assert: 空アカウントは作らず 400 を返す
        self.assertEqual(response.status_code, 400)
        create_mock.assert_not_called()
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.stripe_account_id)

    @patch('business_owners.views.stripe.Account.modify')
    @patch('business_owners.views.stripe.Account.create')
    def test_create_account_sends_form_data_in_single_create_call(
        self, create_mock, modify_mock
    ) -> None:
        # Arrange: 同意済みの事業者と、フロントエンド形式のフォームデータを用意
        self.profile.public_info_consent_at = timezone.now()
        self.profile.save(update_fields=['public_info_consent_at'])
        create_mock.return_value = _StripeObject(id='acct_new_2')
        self._login()

        # Act: フォームデータ付きで連結アカウント作成APIを呼び出す
        response = self.client.post(
            reverse('stripe_custom_create_account'),
            {
                'business_type': 'individual',
                'product_company': {
                    'company_name': 'テスト屋号',
                    'company_name_kana': 'テストヤゴウ',
                    'support_email': 'support@example.com',
                    'accept_tos': True,
                },
                'rep_info': {
                    'last_name_kanji': '山田',
                    'first_name_kanji': '太郎',
                    'last_name_kana': 'ヤマダ',
                    'first_name_kana': 'タロウ',
                    'rep_email': 'rep@example.com',
                    'rep_phone': '08012345678',
                    'rep_dob': {'year': 1990, 'month': 1, 'day': 2},
                },
                'bank_info': {
                    'bank_code': '0001',
                    'branch_code': '001',
                    'account_type': 'futsu',
                    'account_number': '1234567',
                    'account_holder_name': 'ヤマダタロウ',
                },
                'product_details': {
                    'product_url': 'https://example.com',
                    'product_description': 'テスト用の配送サービス',
                    'product_mcc': '4215',
                },
            },
            format='json',
            HTTP_USER_AGENT='test-agent',
        )

        # Assert: 201 で作成され、フォーム内容が1回の Account.create にまとめて渡される
        self.assertEqual(response.status_code, 201)
        modify_mock.assert_not_called()
        create_mock.assert_called_once()
        _, create_kwargs = create_mock.call_args
        self.assertEqual(create_kwargs['type'], 'custom')
        self.assertEqual(create_kwargs['country'], 'JP')
        self.assertEqual(create_kwargs['business_type'], 'individual')
        self.assertEqual(create_kwargs['individual']['last_name_kanji'], '山田')
        self.assertEqual(
            create_kwargs['external_account']['routing_number'], '0001001'
        )
        self.assertEqual(
            create_kwargs['business_profile']['url'], 'https://example.com'
        )
        self.assertIn('tos_acceptance', create_kwargs)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.stripe_account_id, 'acct_new_2')

    @patch('business_owners.views.stripe.Account.modify')
    @patch('business_owners.views.stripe.Account.create')
    def test_create_account_rewrites_localhost_product_url(
        self, create_mock, modify_mock
    ) -> None:
        # Arrange: 開発環境で自動入力されうる localhost URL を含むフォーム
        self.profile.public_info_consent_at = timezone.now()
        self.profile.save(update_fields=['public_info_consent_at'])
        create_mock.return_value = _StripeObject(id='acct_localhost')
        self._login()

        # Act: localhost の product_url 付きで作成する
        response = self.client.post(
            reverse('stripe_custom_create_account'),
            {
                'business_type': 'individual',
                'product_company': {
                    'company_name': 'テスト屋号',
                    'company_name_kana': 'テストヤゴウ',
                    'support_email': 'support@example.com',
                    'accept_tos': True,
                },
                'rep_info': {
                    'last_name_kanji': '山田',
                    'first_name_kanji': '太郎',
                    'last_name_kana': 'ヤマダ',
                    'first_name_kana': 'タロウ',
                    'rep_email': 'rep@example.com',
                    'rep_phone': '08012345678',
                    'rep_dob': {'year': 1990, 'month': 1, 'day': 2},
                },
                'bank_info': {
                    'bank_code': '0001',
                    'branch_code': '001',
                    'account_type': 'futsu',
                    'account_number': '1234567',
                    'account_holder_name': 'ヤマダタロウ',
                },
                'product_details': {
                    'product_url': (
                        f'http://localhost:3000?subdomain={self.profile.subdomain}'
                    ),
                    'product_description': 'テスト用の配送サービス',
                    'product_mcc': '4215',
                },
            },
            format='json',
            HTTP_USER_AGENT='test-agent',
        )

        # Assert: Stripe には公開ドメイン URL が渡る
        self.assertEqual(response.status_code, 201)
        modify_mock.assert_not_called()
        create_mock.assert_called_once()
        _, create_kwargs = create_mock.call_args
        self.assertEqual(
            create_kwargs['business_profile']['url'],
            f'https://{self.profile.subdomain}.luggo.delivery',
        )

    @patch('business_owners.views.stripe.Account.create')
    def test_create_account_returns_400_for_invalid_form_data(
        self, create_mock
    ) -> None:
        # Arrange: 同意済みの事業者と、不正なフォームデータ（URL形式エラー）を用意
        self.profile.public_info_consent_at = timezone.now()
        self.profile.save(update_fields=['public_info_consent_at'])
        self._login()

        # Act: 不正なフォームデータ付きで連結アカウント作成APIを呼び出す
        response = self.client.post(
            reverse('stripe_custom_create_account'),
            {
                'business_type': 'individual',
                'product_details': {'product_url': 'not-a-url'},
            },
            format='json',
        )

        # Assert: アカウントを作成せずに 400 とバリデーションエラーを返す
        self.assertEqual(response.status_code, 400)
        self.assertIn('valid_errs', response.data)
        create_mock.assert_not_called()
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.stripe_account_id)

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
        # stripe-python v15 相当（dict 非互換・to_dict 必須）のオブジェクトで返す
        class _Account:
            id = 'acct_existing'
            business_type = 'individual'
            charges_enabled = True

            def to_dict(self):
                return {
                    'id': self.id,
                    'business_type': self.business_type,
                    'charges_enabled': self.charges_enabled,
                }

        self.profile.stripe_account_id = 'acct_existing'
        self.profile.save(update_fields=['stripe_account_id'])
        retrieve_mock.return_value = _Account()
        self._login()

        # Act: 連結アカウント取得APIを呼び出す
        response = self.client.get(reverse('stripe_custom_get_account'))

        # Assert: 自社のアカウント情報が JSON として返る
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['account_id'], 'acct_existing')
        self.assertEqual(response.data['account']['charges_enabled'], True)
        retrieve_mock.assert_called_once_with('acct_existing')

    def test_get_account_returns_404_before_onboarding(self) -> None:
        # Arrange: 連結アカウント未作成のままログインする
        self._login()

        # Act: 連結アカウント取得APIを呼び出す
        response = self.client.get(reverse('stripe_custom_get_account'))

        # Assert: 404 になる
        self.assertEqual(response.status_code, 404)
