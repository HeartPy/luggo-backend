"""project 共通 API（ヘルスチェック）のテスト"""
import re

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from project.tenant_origins import TENANT_SUBDOMAIN_ORIGIN_REGEX


class TenantSubdomainOriginRegexTests(SimpleTestCase):
    """本番 CORS 用の事業者サブドメイン正規表現"""

    def test_allows_valid_tenant_origin(self) -> None:
        # Act: 3〜12 文字の事業者サブドメイン Origin を正規表現で照合
        match = re.fullmatch(TENANT_SUBDOMAIN_ORIGIN_REGEX, "https://acme.luggo.delivery")

        # Assert: 許可対象としてマッチする
        self.assertIsNotNone(match)

    def test_rejects_too_short_subdomain(self) -> None:
        # Act: 2 文字の短すぎるサブドメイン Origin を正規表現で照合
        match = re.fullmatch(TENANT_SUBDOMAIN_ORIGIN_REGEX, "https://ab.luggo.delivery")

        # Assert: マッチしない
        self.assertIsNone(match)

    def test_rejects_foreign_domain(self) -> None:
        # Act: luggo.delivery 以外のドメイン Origin を正規表現で照合
        match = re.fullmatch(
            TENANT_SUBDOMAIN_ORIGIN_REGEX,
            "https://acme.evil.com",
        )

        # Assert: マッチしない
        self.assertIsNone(match)


class HealthCheckTests(TestCase):
    """外形監視用ヘルスチェック"""

    def test_health_returns_ok(self) -> None:
        # Act: 認証なしでヘルスチェックへアクセス
        response = self.client.get(reverse('health'))

        # Assert: DB 疎通が確認でき 200 が返る
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ok'})
