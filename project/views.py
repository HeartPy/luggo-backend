from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.request import Request
from rest_framework import status
from django.middleware.csrf import get_token
from django.contrib.sessions.models import Session
from django.utils import timezone
from django.conf import settings
import logging

logger = logging.getLogger(__name__)


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
    # セッションタイプを取得（デフォルトは一時セッション）
    session_type = request.data.get('session_type', 'temporary')

    # セッションタイプに応じて有効期限を設定
    if session_type == 'temporary':
        # 一時セッション（予約フロー、アカウント登録、Stripe設定等）：30分
        request.session.set_expiry(settings.TEMPORARY_SESSION_COOKIE_AGE)
    else:
        # 通常のセッション（ログイン用）：1時間
        request.session.set_expiry(settings.SESSION_COOKIE_AGE)

    request.session.save()

    session_key = request.session.session_key
    if session_key:
        try:
            session = Session.objects.get(session_key=session_key)
            expire_date = session.expire_date
            return Response({
                "sessionStarted": True,
                "expiresAt": expire_date.isoformat() if expire_date else None,
                "sessionType": session_type,
            }, status=status.HTTP_200_OK)
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

        # セッションの有効期限をチェック
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

        return Response({
            "valid": True,
            "expiresAt": session.expire_date.isoformat() if session.expire_date else None,
            "remainingSeconds": remaining_seconds,
        }, status=status.HTTP_200_OK)
    except Session.DoesNotExist:
        return Response({
            "valid": False,
            "message": "セッションが見つかりません",
        }, status=status.HTTP_200_OK)
