"""project 共通 API（ヘルスチェック）のテスト"""
import re
from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from project.tenant_origins import TENANT_SUBDOMAIN_ORIGIN_REGEX
from project.turnstile import verify_turnstile


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


class VerifyTurnstileTests(SimpleTestCase):
    """Cloudflare Turnstile トークン検証"""

    @override_settings(TURNSTILE_SECRET_KEY='', DEBUG=True)
    def test_missing_secret_allows_in_debug(self) -> None:
        # Assert: Secret 未設定でも開発環境では許可される
        self.assertTrue(verify_turnstile('any-token'))

    @override_settings(TURNSTILE_SECRET_KEY='', DEBUG=False)
    def test_missing_secret_rejects_in_production(self) -> None:
        # Assert: Secret 未設定の本番相当環境では拒否される
        self.assertFalse(verify_turnstile('any-token'))

    @override_settings(TURNSTILE_SECRET_KEY='1x0000000000000000000000000000000AA')
    def test_dummy_pass_secret_always_passes(self) -> None:
        # Assert: テスト用「常に成功」Secret はネットワークなしで許可される
        self.assertTrue(verify_turnstile(''))

    @override_settings(TURNSTILE_SECRET_KEY='2x0000000000000000000000000000000AA')
    def test_dummy_fail_secret_always_fails(self) -> None:
        # Assert: テスト用「常に失敗」Secret は拒否される
        self.assertFalse(verify_turnstile('any-token'))

    @override_settings(TURNSTILE_SECRET_KEY='real-secret-key')
    def test_empty_token_rejected_without_network_call(self) -> None:
        # Act: 本物の Secret でトークンが空のまま検証
        with patch('project.turnstile.requests.post') as post_mock:
            result = verify_turnstile('')

        # Assert: Siteverify を呼ばずに拒否される
        self.assertFalse(result)
        post_mock.assert_not_called()

    @override_settings(TURNSTILE_SECRET_KEY='real-secret-key')
    def test_siteverify_success_passes(self) -> None:
        # Arrange: Siteverify が success を返す
        response_mock = MagicMock()
        response_mock.json.return_value = {'success': True}

        # Act: 有効なトークンと IP で検証する
        with patch('project.turnstile.requests.post', return_value=response_mock):
            result = verify_turnstile('valid-token', '203.0.113.1')

        # Assert: 検証が成功する
        self.assertTrue(result)

    @override_settings(TURNSTILE_SECRET_KEY='real-secret-key')
    def test_siteverify_failure_rejected(self) -> None:
        # Arrange: Siteverify が失敗を返す
        response_mock = MagicMock()
        response_mock.json.return_value = {
            'success': False,
            'error-codes': ['invalid-input-response'],
        }

        # Act: 不正なトークンで検証する
        with patch('project.turnstile.requests.post', return_value=response_mock):
            result = verify_turnstile('bad-token')

        # Assert: 検証が拒否される
        self.assertFalse(result)

    @override_settings(TURNSTILE_SECRET_KEY='real-secret-key')
    def test_network_error_rejected(self) -> None:
        # Arrange: Siteverify への通信が失敗する
        with patch(
            'project.turnstile.requests.post',
            side_effect=requests.ConnectionError,
        ):
            # Act: 通信エラー時に検証する
            result = verify_turnstile('valid-token')

        # Assert: 通信エラー時も許可せず拒否される
        self.assertFalse(result)


class HealthCheckTests(TestCase):
    """外形監視用ヘルスチェック"""

    def test_health_returns_ok(self) -> None:
        # Act: 認証なしでヘルスチェックへアクセス
        response = self.client.get(reverse('health'))

        # Assert: DB 疎通が確認でき 200 が返る
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ok'})
