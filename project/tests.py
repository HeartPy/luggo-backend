"""project 共通 API（ヘルスチェック）のテスト"""
import os
import re
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from project.settings import _config_from_env_file, _env_filename_for_settings_module
from project.tenant_origins import TENANT_SUBDOMAIN_ORIGIN_REGEX
from project.throttling import (
    LoginRateThrottle,
    PublicReadRateThrottle,
    ScopedIPRateThrottle,
)
from project.turnstile import verify_turnstile
from users.utils import get_client_ip


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


class EnvConfigLoadingTests(SimpleTestCase):
    """settings の .env 読み込み（ファイル欠落時は OS 環境変数へフォールバック）"""

    def test_prod_settings_reads_production_env_file(self) -> None:
        # Act: settings モジュール名から読む .env ファイル名を決める
        filename = _env_filename_for_settings_module('project.prod_settings')

        # Assert: 本番用ファイル名になる
        self.assertEqual(filename, '.env.production')

    def test_dev_settings_reads_development_env_file(self) -> None:
        # Act: settings モジュール名から読む .env ファイル名を決める
        filename = _env_filename_for_settings_module('project.settings')

        # Assert: 開発用ファイル名になる
        self.assertEqual(filename, '.env.development')

    def test_missing_env_file_reads_os_environ(self) -> None:
        # Arrange: .env が無い一時ディレクトリと OS 環境変数
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {'LUGGO_TEST_ENV_FALLBACK': 'from-os'}):
                # Act: 欠落した .env を指定して Config を作る
                config = _config_from_env_file('.env.development', base_dir=Path(tmp))

                # Assert: ファイルではなく OS 環境変数から読める
                self.assertEqual(config('LUGGO_TEST_ENV_FALLBACK'), 'from-os')

    def test_present_env_file_is_loaded(self) -> None:
        # Arrange: 一時ディレクトリに .env.development を置く
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / '.env.development').write_text(
                'LUGGO_TEST_ENV_FILE=from-file\n',
                encoding='utf-8',
            )

            # Act: 存在する .env を指定して Config を作る
            config = _config_from_env_file('.env.development', base_dir=Path(tmp))

            # Assert: OS 環境変数ではなくファイルから読める
            self.assertNotIn('LUGGO_TEST_ENV_FILE', os.environ)
            self.assertEqual(config('LUGGO_TEST_ENV_FILE'), 'from-file')


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


class GetClientIpTests(SimpleTestCase):
    """クライアント IP の取得（X-Forwarded-For は末尾を採用）"""

    def setUp(self) -> None:
        self.factory = RequestFactory()

    def test_uses_remote_addr_without_xff(self) -> None:
        # Act: X-Forwarded-For なしのリクエスト
        request = self.factory.get('/', REMOTE_ADDR='192.0.2.10')

        # Assert: REMOTE_ADDR が返る
        self.assertEqual(get_client_ip(request), '192.0.2.10')

    def test_uses_single_xff_value(self) -> None:
        # Act: ALB が付与した X-Forwarded-For が 1 件
        request = self.factory.get('/', HTTP_X_FORWARDED_FOR='203.0.113.5')

        # Assert: その値が返る
        self.assertEqual(get_client_ip(request), '203.0.113.5')

    def test_ignores_spoofed_first_xff_entry(self) -> None:
        # Arrange: クライアントが偽の XFF を付け、ALB が実 IP を末尾に追記した状態
        request = self.factory.get(
            '/',
            HTTP_X_FORWARDED_FOR='1.2.3.4, 203.0.113.5',
        )

        # Assert: 偽装可能な先頭ではなく、ALB が追記した末尾を採用する
        self.assertEqual(get_client_ip(request), '203.0.113.5')


def _low_throttle_rates():
    """
    throttle テスト用にレートを下げる。

    DRF の THROTTLE_RATES は import 時に settings のレート dict を参照する
    クラス属性のため、override_settings(REST_FRAMEWORK=...) では変わらない。
    patch.dict で dict の中身を直接差し替える（終了時に自動復元される）。
    """
    return patch.dict(
        ScopedIPRateThrottle.THROTTLE_RATES,
        {
            'login': '2/min',
            'email-send': '2/min',
            'public-read': '2/min',
            'payment': '2/min',
        },
    )


class ThrottleTests(TestCase):
    """公開 API の IP 単位レート制限"""

    def setUp(self) -> None:
        cache.clear()
        self.factory = RequestFactory()

    def tearDown(self) -> None:
        cache.clear()

    @override_settings(THROTTLE_ENABLED=True)
    def test_exceeding_rate_returns_429(self) -> None:
        with _low_throttle_rates():
            # Act: レート上限（2/min）まではアクセスできる
            url = reverse('verify_password_reset_token')
            for _ in range(2):
                response = self.client.get(url)
                self.assertNotEqual(response.status_code, 429)

            # Act: 上限超過
            response = self.client.get(url)

            # Assert: 429 が返る
            self.assertEqual(response.status_code, 429)

    def test_disabled_by_default_never_throttles(self) -> None:
        with _low_throttle_rates():
            # Act: THROTTLE_ENABLED がデフォルト（無効）のまま上限を超えてアクセス
            url = reverse('verify_password_reset_token')
            for _ in range(5):
                response = self.client.get(url)

                # Assert: 429 にならない
                self.assertNotEqual(response.status_code, 429)

    @override_settings(THROTTLE_ENABLED=True)
    def test_scopes_have_independent_counters(self) -> None:
        with _low_throttle_rates():
            # Arrange: login スコープの上限を使い切る
            request = self.factory.get('/', REMOTE_ADDR='192.0.2.10')
            self.assertTrue(LoginRateThrottle().allow_request(request, None))
            self.assertTrue(LoginRateThrottle().allow_request(request, None))
            self.assertFalse(LoginRateThrottle().allow_request(request, None))

            # Assert: 別スコープ（public-read）は影響を受けない
            self.assertTrue(PublicReadRateThrottle().allow_request(request, None))

    @override_settings(THROTTLE_ENABLED=True)
    def test_different_ips_have_independent_counters(self) -> None:
        with _low_throttle_rates():
            # Arrange: IP その1 が上限を使い切る
            request_a = self.factory.get('/', REMOTE_ADDR='192.0.2.10')
            self.assertTrue(LoginRateThrottle().allow_request(request_a, None))
            self.assertTrue(LoginRateThrottle().allow_request(request_a, None))
            self.assertFalse(LoginRateThrottle().allow_request(request_a, None))

            # Assert: 別 IP は制限されない
            request_b = self.factory.get('/', REMOTE_ADDR='198.51.100.20')
            self.assertTrue(LoginRateThrottle().allow_request(request_b, None))


class HealthCheckTests(TestCase):
    """外形監視用ヘルスチェック"""

    def test_health_returns_ok(self) -> None:
        # Act: 認証なしでヘルスチェックへアクセス
        response = self.client.get(reverse('health'))

        # Assert: DB 疎通が確認でき 200 が返る
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ok'})

    @override_settings(ALLOWED_HOSTS=['api.luggo.delivery'])
    def test_health_allows_alb_private_ip_host(self) -> None:
        # Arrange / Act: ALB と同様に Host がプライベート IP
        response = self.client.get(
            '/api/common/health',
            HTTP_HOST='10.0.25.95',
        )

        # Assert: ALLOWED_HOSTS 外でも 400 にならず 200
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ok'})

    @override_settings(ALLOWED_HOSTS=['api.luggo.delivery'])
    def test_health_allows_head_for_uptime_monitors(self) -> None:
        # Arrange / Act: UptimeRobot 無料枠と同様に HEAD + プライベート IP Host
        response = self.client.head(
            '/api/common/health',
            HTTP_HOST='10.0.25.95',
        )

        # Assert: 405 にならず 200（本文なし）
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'')

    @override_settings(ALLOWED_HOSTS=['api.luggo.delivery'])
    def test_other_paths_still_reject_disallowed_host(self) -> None:
        # Act: ヘルス以外は従来どおり Host 検証する
        response = self.client.get(
            '/api/common/csrf',
            HTTP_HOST='10.0.25.95',
        )

        # Assert: DisallowedHost → 400
        self.assertEqual(response.status_code, 400)
