from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.deprecation import MiddlewareMixin


class SlidingSessionMiddleware(MiddlewareMixin):
    """
    認証済みのリクエストごとに有効期限を更新し、最終アクセスからログインを維持する。

    事業者・管理者は1時間、配達者は1週間と、ユーザー種別によって
    有効期限が異なる。

    一時セッション（Stripe設定等）はセッション内の別タイムスタンプで管理し、
    ログインセッション自体の有効期限には影響しない。
    """

    def process_request(self, request):
        if getattr(request, "user", None) and request.user.is_authenticated:
            if request.session.session_key:
                if getattr(request.user, "user_type", None) == "delivery_driver":
                    session_age = settings.DRIVER_SESSION_COOKIE_AGE
                else:
                    session_age = settings.SESSION_COOKIE_AGE
                request.session.set_expiry(session_age)

                if 'temporary_session_expires_at' in request.session:
                    new_expires_at = timezone.now() + timedelta(seconds=settings.TEMPORARY_SESSION_COOKIE_AGE)
                    request.session['temporary_session_expires_at'] = new_expires_at.isoformat()
