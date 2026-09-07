"""Stripe Payment Method Domain（Apple Pay 用ドメイン）自動登録のテスト"""
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from business_owners.models import RegistrationToken
from business_owners.payment_method_domains import (
    build_tenant_domain,
    ensure_payment_method_domain,
)
from business_owners.utils import generate_registration_token


class BuildTenantDomainTests(TestCase):
    """FRONTEND_BASE_URL からのテナントドメイン導出のテスト"""

    @override_settings(FRONTEND_BASE_URL='https://www.luggo.delivery')
    def test_builds_domain_from_production_url(self) -> None:
        # Act: 本番 URL からテナントドメインを導出
        domain = build_tenant_domain('sampleco')

        # Assert: ベースドメインの先頭にサブドメインが付く
        self.assertEqual(domain, 'sampleco.luggo.delivery')

    @override_settings(FRONTEND_BASE_URL='https://luggo.delivery')
    def test_builds_domain_from_apex_url(self) -> None:
        # Act: サブドメインなしのベース URL から導出
        domain = build_tenant_domain('sampleco')

        # Assert: そのままベースドメインに付与される
        self.assertEqual(domain, 'sampleco.luggo.delivery')

    @override_settings(FRONTEND_BASE_URL='http://localhost:3000')
    def test_returns_none_for_localhost(self) -> None:
        # Act: 開発環境の URL から導出
        domain = build_tenant_domain('sampleco')

        # Assert: 登録スキップのため None が返る
        self.assertIsNone(domain)


class EnsurePaymentMethodDomainTests(TestCase):
    """Payment Method Domain の冪等登録のテスト"""

    @patch('business_owners.payment_method_domains.stripe.PaymentMethodDomain')
    def test_creates_and_validates_new_domain(self, domain_api) -> None:
        # Arrange: 未登録ドメイン。作成直後は apple_pay が inactive
        domain_api.list.return_value = {'data': []}
        domain_api.create.return_value = {
            'id': 'pmd_new',
            'apple_pay': {'status': 'inactive'},
        }

        # Act: 冪等登録を実行
        ensure_payment_method_domain('sampleco.luggo.delivery')

        # Assert: create と validate が呼ばれる
        domain_api.create.assert_called_once_with(
            domain_name='sampleco.luggo.delivery'
        )
        domain_api.validate.assert_called_once_with('pmd_new')

    @patch('business_owners.payment_method_domains.stripe.PaymentMethodDomain')
    def test_skips_create_when_already_registered(self, domain_api) -> None:
        # Arrange: 登録済みで apple_pay も active なドメイン
        domain_api.list.return_value = {
            'data': [{'id': 'pmd_existing', 'apple_pay': {'status': 'active'}}]
        }

        # Act: 冪等登録を実行
        ensure_payment_method_domain('sampleco.luggo.delivery')

        # Assert: create も validate も呼ばれない
        domain_api.create.assert_not_called()
        domain_api.validate.assert_not_called()

    @patch('business_owners.payment_method_domains.stripe.PaymentMethodDomain')
    def test_revalidates_existing_inactive_domain(self, domain_api) -> None:
        # Arrange: 登録済みだが apple_pay が inactive のドメイン
        domain_api.list.return_value = {
            'data': [{'id': 'pmd_existing', 'apple_pay': {'status': 'inactive'}}]
        }

        # Act: 冪等登録を実行
        ensure_payment_method_domain('sampleco.luggo.delivery')

        # Assert: create はスキップし validate のみ実行される
        domain_api.create.assert_not_called()
        domain_api.validate.assert_called_once_with('pmd_existing')


class RegistrationEnqueueTests(TestCase):
    """事業者登録時のドメイン登録タスク投入のテスト"""

    EMAIL = 'domain-enqueue@example.com'

    def setUp(self) -> None:
        self.client = APIClient()

    def _register_payload(self, token: str) -> dict:
        return {
            'token': token,
            'business_type': 'individual',
            'company_name': 'ドメイン登録テスト運送',
            'rep_last_name': '山田',
            'rep_first_name': '太郎',
            'rep_last_name_kana': 'ヤマダ',
            'rep_first_name_kana': 'タロウ',
            'email': self.EMAIL,
            'phone': '0312345678',
            'subdomain': 'domainowner',
            'password': 'StrongPass123!',
        }

    def _issue_token(self) -> RegistrationToken:
        return generate_registration_token(self.EMAIL)

    @patch('business_owners.views.register_payment_method_domain.delay')
    @patch('business_owners.views.send_registration_completed_emails')
    def test_enqueues_domain_registration_on_register(
        self, _send_mail, delay_mock
    ) -> None:
        # Arrange: 有効な登録トークンを発行
        token = self._issue_token()

        # Act: 事業者アカウント登録を実行
        response = self.client.post(
            reverse('register_business_account'),
            self._register_payload(token.token),
            format='json',
        )

        # Assert: 登録成功し、サブドメインでタスクが投入される
        self.assertEqual(response.status_code, 201)
        delay_mock.assert_called_once_with('domainowner')

    @patch(
        'business_owners.views.register_payment_method_domain.delay',
        side_effect=Exception('broker down'),
    )
    @patch('business_owners.views.send_registration_completed_emails')
    def test_registration_succeeds_even_if_enqueue_fails(
        self, _send_mail, _delay_mock
    ) -> None:
        # Arrange: 有効な登録トークンを発行
        token = self._issue_token()

        # Act: タスク投入が失敗する状態で登録を実行
        response = self.client.post(
            reverse('register_business_account'),
            self._register_payload(token.token),
            format='json',
        )

        # Assert: 登録自体は成功する
        self.assertEqual(response.status_code, 201)
