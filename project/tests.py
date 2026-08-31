"""project 共通 API（ヘルスチェック）のテスト"""
from django.test import TestCase
from django.urls import reverse


class HealthCheckTests(TestCase):
    """外形監視用ヘルスチェック"""

    def test_health_returns_ok(self) -> None:
        # Act: 認証なしでヘルスチェックへアクセス
        response = self.client.get(reverse('health'))

        # Assert: DB 疎通が確認でき 200 が返る
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ok'})
