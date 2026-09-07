"""
公開 API 向けの IP 単位レート制限（DRF throttle）

- カウンタは default キャッシュに保存する。本番は ROUTING_CACHE_URL（Redis）が
  default キャッシュのため、ECS の複数タスク間で共有される。
- settings.THROTTLE_ENABLED が False のときは何も制限しない
  （デフォルト: 本番のみ有効。開発・ユニットテスト・E2E には影響しない）。
- レートは settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES'] のスコープ別設定を使う。

既存のログイン失敗 IP ブロック（users.utils.check_ip_login_attempts）や
Turnstile 検証はそのまま残し、この throttle は「成否に関わらずリクエスト回数
そのものを抑える」補完レイヤーとして機能する。
"""
from django.conf import settings
from rest_framework.throttling import SimpleRateThrottle

from users.utils import get_client_ip


class ScopedIPRateThrottle(SimpleRateThrottle):
    """クライアント IP + スコープ単位で回数制限する基底クラス"""

    def allow_request(self, request, view) -> bool:
        if not getattr(settings, 'THROTTLE_ENABLED', False):
            return True
        return super().allow_request(request, view)

    def get_cache_key(self, request, view) -> str:
        return self.cache_format % {
            'scope': self.scope,
            'ident': get_client_ip(request) or 'unknown',
        }


class LoginRateThrottle(ScopedIPRateThrottle):
    """ログイン系（認証コード送信・検証、配達者ログイン、登録完了）"""
    scope = 'login'


class EmailSendRateThrottle(ScopedIPRateThrottle):
    """メール送信を伴う API（パスワード再設定・登録メール）"""
    scope = 'email-send'


class PublicReadRateThrottle(ScopedIPRateThrottle):
    """公開参照系（予約照会、トークン検証）"""
    scope = 'public-read'


class PaymentRateThrottle(ScopedIPRateThrottle):
    """決済系（PaymentIntent 作成）"""
    scope = 'payment'
