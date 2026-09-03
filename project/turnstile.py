"""Cloudflare Turnstile のサーバー側トークン検証"""
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

TURNSTILE_VERIFY_URL = 'https://challenges.cloudflare.com/turnstile/v0/siteverify'

# Cloudflare 公式のテスト用 Secret Key。
# ローカル開発・ユニットテスト・E2E ではネットワークを介さず固定結果を返す。
_TEST_SECRET_RESULTS = {
    '1x0000000000000000000000000000000AA': True,   # 常に成功
    '2x0000000000000000000000000000000AA': False,  # 常に失敗
    '3x0000000000000000000000000000000AA': False,  # トークン使用済み
}


def verify_turnstile(token: object, remote_ip: str = '') -> bool:
    """
    Turnstile トークンを Siteverify API で検証

    - Secret Key 未設定: 開発環境（DEBUG）のみ許可し、本番では拒否する
    - テスト用 Secret Key: ネットワークを介さず固定結果を返す
    - 検証失敗・通信エラー時は許可せず拒否する
    """
    secret = settings.TURNSTILE_SECRET_KEY

    if not secret:
        if not settings.DEBUG:
            logger.error(
                "TURNSTILE_SECRET_KEY が未設定のためリクエストを拒否しました"
            )
        return settings.DEBUG

    if secret in _TEST_SECRET_RESULTS:
        return _TEST_SECRET_RESULTS[secret]

    if not token or not isinstance(token, str):
        return False

    data = {'secret': secret, 'response': token}
    if remote_ip:
        data['remoteip'] = remote_ip

    try:
        response = requests.post(TURNSTILE_VERIFY_URL, data=data, timeout=5)
        result = response.json()
    except (requests.RequestException, ValueError):
        logger.error("Turnstile 検証リクエストに失敗しました", exc_info=True)
        return False

    if not result.get('success', False):
        logger.warning(
            "Turnstile 検証失敗: error_codes=%s",
            result.get('error-codes', []),
        )
        return False

    return True
