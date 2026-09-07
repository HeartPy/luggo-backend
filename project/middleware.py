from datetime import timedelta

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.deprecation import MiddlewareMixin

from project.health import health_payload

# ALB / 外形監視が叩くパス（urls_common の health と一致させる）
HEALTH_CHECK_PATH = "/api/common/health"


class HealthCheckMiddleware:
    """
    ALB のヘルスチェックは Host にターゲットのプライベート IP を付ける。
    CommonMiddleware の ALLOWED_HOSTS 検証より前に応答し、
    ALLOWED_HOSTS を api.luggo.delivery のみに保てるようにする。
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.method in ("GET", "HEAD") and request.path == HEALTH_CHECK_PATH:
            body, status_code = health_payload()
            # UptimeRobot 無料枠は HEAD 固定。本文は付けない
            if request.method == "HEAD":
                return HttpResponse(
                    status=status_code,
                    content_type="application/json",
                )
            return JsonResponse(body, status=status_code)
        return self.get_response(request)


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
