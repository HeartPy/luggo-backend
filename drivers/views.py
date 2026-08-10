import logging

from django.conf import settings
from django.contrib.auth import authenticate, login
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response

from users.utils import (
    check_ip_login_attempts,
    get_client_ip,
    record_login_failure,
    record_login_success,
)

logger = logging.getLogger(__name__)


@api_view(["POST"])
@permission_classes([AllowAny])
def driver_login(request: Request) -> Response:
    """配達者専用ログインAPI（二段階認証なし）"""
    try:
        # IPアドレスベースの制限をチェック
        ip_address = get_client_ip(request)
        is_blocked, err_msg = check_ip_login_attempts(ip_address)
        if is_blocked:
            logger.warning(f"配達者ログイン試行がブロックされました: ip={ip_address}")
            return Response(
                {'error': err_msg},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )

        email = request.data.get('email', '').strip().lower()
        password = request.data.get('password', '')

        if not email or not password:
            return Response(
                {'error': 'メールアドレスとパスワードを入力してください。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # ユーザー認証（配達者アカウント以外はログインさせない）
        user = authenticate(request, username=email, password=password)
        if not user or user.user_type != 'delivery_driver':
            record_login_failure(ip_address)
            # セキュリティ上の理由で、ユーザーが存在しない場合・パスワード相違・
            # 配達者以外のアカウントである場合を区別しない
            return Response(
                {'error': 'メールアドレスまたはパスワードが正しくありません。'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # ログイン処理（確認コードによる二段階認証は不要）
        login(request, user)

        # ログインセッションの有効期限を1週間に明示的に設定
        request.session.set_expiry(settings.DRIVER_SESSION_COOKIE_AGE)
        request.session.save()

        # ログイン成功時にIPアドレスの失敗カウントをクリア
        record_login_success(ip_address)

        logger.info(f"配達者ログイン成功: email={email}, user_id={user.id}")

        return Response({
            'message': 'ログインに成功しました。',
            'user_id': str(user.id),
            'email': user.email,
            'user_type': user.user_type,
            'dashboard_url': user.get_dashboard_url(),
        }, status=status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"配達者ログインエラー: error={str(e)}", exc_info=True)
        return Response(
            {'error': '予期しないエラーが発生しました。しばらく時間をおいて再度お試しください。'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
