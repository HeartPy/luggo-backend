from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.sessions.models import Session
from django.middleware.csrf import get_token
from django.utils import timezone
from django.views.decorators.cache import never_cache
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response

from project.health import health_payload


@never_cache
@api_view(["GET", "HEAD"])
@permission_classes([AllowAny])
def health(request: Request) -> Response:
    """
    外形監視用ヘルスチェック（DB 疎通のみ確認）

    本番の ALB / UptimeRobot は HealthCheckMiddleware が先に応答する。
    HEAD は外形監視（UptimeRobot 無料枠）向け。このビューは名前付き URL 用。
    """
    body, status_code = health_payload()
    return Response(body, status=status_code)


@api_view(["GET"])
@permission_classes([AllowAny])
def get_csrf_token(request: Request) -> Response:
    """CSRFトークンを生成（クッキーも自動的に設定される）"""
    token = get_token(request)
    response = Response({"csrfToken": token})
    # ミドルウェアが処理するが、念のため明示的に設定
    response.set_cookie(
        'csrftoken',
        token,
        max_age=settings.SESSION_COOKIE_AGE,
        httponly=False,
        samesite='Lax',
        secure=request.is_secure(),
    )
    return response


@api_view(["POST"])
@permission_classes([AllowAny])
def start_session(request: Request) -> Response:
    """セッションを開始"""
    session_type = request.data.get('session_type', 'temporary')

    # セッション自体の有効期限は常に SESSION_COOKIE_AGEで統一
    # ログイン状態を一時セッションの期限切れで失わないようにするため
    request.session.set_expiry(settings.SESSION_COOKIE_AGE)

    if session_type == 'temporary':
        # 一時セッション（Stripe設定、予約フロー等）の有効期限をセッション内に別管理
        temporary_expires_at = timezone.now() + timedelta(seconds=settings.TEMPORARY_SESSION_COOKIE_AGE)
        request.session['temporary_session_expires_at'] = temporary_expires_at.isoformat()
    else:
        # 通常セッション開始時は一時セッション情報をクリア
        request.session.pop('temporary_session_expires_at', None)

    request.session.save()

    session_key = request.session.session_key
    if session_key:
        try:
            session = Session.objects.get(session_key=session_key)
            expire_date = session.expire_date

            response_data: dict = {
                "sessionStarted": True,
                "expiresAt": expire_date.isoformat() if expire_date else None,
                "sessionType": session_type,
            }
            if session_type == 'temporary':
                response_data["temporaryExpiresAt"] = request.session['temporary_session_expires_at']

            return Response(response_data, status=status.HTTP_200_OK)
        except Session.DoesNotExist:
            pass

    return Response({
        "sessionStarted": True,
        "sessionType": session_type,
    }, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([AllowAny])
def check_session_validity(request: Request) -> Response:
    """セッションの有効性をチェック"""
    session_key = request.session.session_key
    if not session_key:
        return Response({
            "valid": False,
            "message": "セッションが開始されていません",
        }, status=status.HTTP_200_OK)

    try:
        session = Session.objects.get(session_key=session_key)
        now = timezone.now()

        # セッション自体（ログイン）の有効期限をチェック
        if session.expire_date and session.expire_date <= now:
            return Response({
                "valid": False,
                "message": "セッションの有効期限が切れています",
                "expired": True,
            }, status=status.HTTP_200_OK)

        # セッションの残り時間を計算
        remaining_seconds = None
        if session.expire_date:
            remaining_seconds = int((session.expire_date - now).total_seconds())

        response_data: dict = {
            "valid": True,
            "expiresAt": session.expire_date.isoformat() if session.expire_date else None,
            "remainingSeconds": remaining_seconds,
        }

        # 一時セッション（Stripe設定等）の期限切れを別途チェック
        temporary_expires_at_str = request.session.get('temporary_session_expires_at')
        if temporary_expires_at_str:
            temporary_expires_at = datetime.fromisoformat(temporary_expires_at_str)
            if timezone.is_naive(temporary_expires_at):
                temporary_expires_at = timezone.make_aware(temporary_expires_at)

            if temporary_expires_at <= now:
                response_data["temporarySessionExpired"] = True
                response_data["temporarySessionMessage"] = "セッションの有効期限が切れています。お手数おかけしますが、最初から入力し直してください。"
                request.session.pop('temporary_session_expires_at', None)
                request.session.save()
            else:
                response_data["temporarySessionExpired"] = False
                temporary_remaining = int((temporary_expires_at - now).total_seconds())
                response_data["temporaryExpiresAt"] = temporary_expires_at_str
                response_data["temporaryRemainingSeconds"] = temporary_remaining

        return Response(response_data, status=status.HTTP_200_OK)
    except Session.DoesNotExist:
        return Response({
            "valid": False,
            "message": "セッションが見つかりません",
        }, status=status.HTTP_200_OK)
