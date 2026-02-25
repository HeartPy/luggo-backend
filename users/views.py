from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.request import Request
from rest_framework import status
from django.contrib.auth import authenticate, login, logout, get_user_model
from django.conf import settings
from django.utils import timezone
import logging

from .utils import (
    generate_login_verification_code,
    send_login_verification_code,
    verify_login_code,
    clear_verification_code,
    check_login_session_validity,
    clear_login_session,
    get_client_ip,
    check_ip_login_attempts,
    record_login_failure,
    record_login_success,
    generate_password_reset_token,
    send_password_reset_email,
    verify_password_reset_token,
)
from .validators import validate_password_strength

User = get_user_model()
logger = logging.getLogger(__name__)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def check_authentication(request: Request) -> Response:
    """認証状態をチェックするエンドポイント"""
    return Response({
        "authenticated": True,
        "user_id": str(request.user.id),
        "email": request.user.email,
        "user_type": request.user.user_type,
    }, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([AllowAny])
def send_login_code(request: Request) -> Response:
    """ログイン認証コード送信API"""
    try:
        # IPアドレスベースの制限をチェック
        ip_address = get_client_ip(request)
        is_blocked, error_message = check_ip_login_attempts(ip_address)
        if is_blocked:
            logger.warning(f"ログイン試行がブロックされました: ip={ip_address}")
            return Response(
                {'error': error_message},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )

        email = request.data.get('email', '').strip().lower()
        password = request.data.get('password', '')

        if not email or not password:
            return Response(
                {'error': 'メールアドレスとパスワードを入力してください。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # ユーザー認証
        user = authenticate(request, username=email, password=password)
        if not user:
            # ログイン失敗を記録
            record_login_failure(ip_address)
            # セキュリティ上の理由で、ユーザーが存在しない場合とパスワードが間違っている場合を区別しない
            return Response(
                {'error': 'メールアドレスまたはパスワードが正しくありません。'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # セッション有効期限をチェック（既存のセッションがある場合）
        if user.login_session_started_at and not check_login_session_validity(user):
            # セッションが期限切れの場合はクリア
            clear_login_session(user)
            return Response(
                {'error': 'セッションの有効期限が切れています。最初からログイン手続きをやり直してください。'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # 認証コードを生成
        code = generate_login_verification_code(user)

        # メール送信
        success = send_login_verification_code(user, code)
        if not success:
            logger.error(f"ログイン認証コードメール送信失敗: email={email}")
            return Response(
                {'error': 'メールの送信に失敗しました。しばらく時間をおいて再度お試しください。'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        # ログイン成功（認証コード送信成功）時にIPアドレスの失敗カウントをクリア
        record_login_success(ip_address)

        logger.info(f"ログイン認証コード送信成功: email={email}")
        return Response({
            'message': '認証コードをメールアドレスに送信しました。',
        }, status=status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"ログイン認証コード送信エラー: error={str(e)}", exc_info=True)
        return Response(
            {'error': '予期しないエラーが発生しました。しばらく時間をおいて再度お試しください。'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(["POST"])
@permission_classes([AllowAny])
def verify_login_code_api(request: Request) -> Response:
    """認証コード検証・ログインAPI"""
    try:
        # IPアドレスベースの制限をチェック
        ip_address = get_client_ip(request)
        is_blocked, error_message = check_ip_login_attempts(ip_address)
        if is_blocked:
            logger.warning(f"ログイン試行がブロックされました: ip={ip_address}")
            return Response(
                {'error': error_message},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )

        email = request.data.get('email', '').strip().lower()
        code = request.data.get('code', '').strip()

        if not code:
            return Response(
                {'error': '認証コードを入力してください。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not email:
            # メールアドレスが空の場合はシステムエラー（通常あり得ない）
            logger.error("認証コード検証API: メールアドレスが空")
            return Response(
                {'error': 'システムエラーが発生しました。最初からログイン手続きをやり直してください。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # ユーザーを取得
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            # ログイン失敗を記録
            record_login_failure(ip_address)
            # ステップ1で認証済みなので、ユーザーが見つからないのは異常なケース
            logger.error(f"認証コード検証API: ユーザーが見つかりません: email={email}")
            return Response(
                {'error': 'ユーザーが見つかりません。最初からログイン手続きをやり直してください。'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # セッション有効期限をチェック
        if not check_login_session_validity(user):
            clear_login_session(user)
            clear_verification_code(user)
            return Response(
                {'error': 'セッションの有効期限が切れています。最初からログイン手続きをやり直してください。'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # 認証コードを検証
        if not verify_login_code(user, code):
            # 認証コード検証失敗回数をカウント
            user.verification_code_attempts += 1
            user.save(update_fields=['verification_code_attempts'])

            # ログイン失敗を記録（IPアドレスベース）
            record_login_failure(ip_address)

            # 3回失敗したら認証コードを無効化し、新しい認証コードを要求
            if user.verification_code_attempts >= 3:
                clear_verification_code(user)
                logger.warning(f"認証コード検証失敗回数が上限に達しました: email={email}, attempts={user.verification_code_attempts}")
                return Response(
                    {'error': '認証コードの試行回数が上限に達しました。認証コードを再送信してください。'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            return Response(
                {'error': '認証コードが正しくないか、有効期限が切れています。'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # ログイン処理
        login(request, user)

        # ログインセッションの有効期限を1時間に明示的に設定
        request.session.set_expiry(settings.SESSION_COOKIE_AGE)
        request.session.save()

        # 認証コードとセッションをクリア
        clear_verification_code(user)
        clear_login_session(user)

        # ログイン成功時にIPアドレスの失敗カウントをクリア
        record_login_success(ip_address)

        logger.info(f"ログイン成功: email={email}, user_id={user.id}")

        # ユーザータイプに応じたダッシュボードURLを返す
        dashboard_url = user.get_dashboard_url()

        return Response({
            'message': 'ログインに成功しました。',
            'user_id': str(user.id),
            'email': user.email,
            'user_type': user.user_type,
            'dashboard_url': dashboard_url,
        }, status=status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"認証コード検証エラー: error={str(e)}", exc_info=True)
        return Response(
            {'error': '予期しないエラーが発生しました。しばらく時間をおいて再度お試しください。'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def logout_api(request: Request) -> Response:
    """ログアウト処理"""
    try:
        logout(request)
        return Response(
            {'message': 'ログアウトしました。'},
            status=status.HTTP_200_OK
        )
    except Exception as e:
        logger.error(f"ログアウトエラー: error={str(e)}", exc_info=True)
        return Response(
            {'error': '予期しないエラーが発生しました。'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(["POST"])
@permission_classes([AllowAny])
def request_password_reset(request: Request) -> Response:
    """パスワード再設定メール送信API"""
    try:
        email = request.data.get('email', '').strip().lower()

        if not email:
            return Response(
                {'error': 'メールアドレスを入力してください。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # ユーザーが存在する場合のみメールを送信
        user = User.objects.filter(email=email).first()
        if user:
            password_reset_token = generate_password_reset_token(user)
            success = send_password_reset_email(user, password_reset_token.token)

            if not success:
                logger.error(f"パスワード再設定メール送信失敗: email={email}")
                return Response(
                    {'error': 'メールの送信に失敗しました。しばらく時間をおいて再度お試しください。'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

            logger.info(f"パスワード再設定メール送信成功: email={email}")

        # セキュリティのため、ユーザーの有無に関わらず同じレスポンスを返す
        return Response({
            'message': 'メールを送信しました。メール内のリンクからパスワードを再設定してください。',
        }, status=status.HTTP_200_OK)
    except Exception as e:
        logger.error(f"パスワード再設定メール送信エラー: error={str(e)}", exc_info=True)
        return Response(
            {'error': '予期しないエラーが発生しました。しばらく時間をおいて再度お試しください。'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(["GET"])
@permission_classes([AllowAny])
def verify_password_reset_token_api(request: Request) -> Response:
    """パスワード再設定トークン検証API"""
    try:
        token = request.query_params.get('token', '').strip()

        if not token:
            logger.warning("パスワード再設定トークン検証: トークンが空")
            return Response(
                {'error': 'リンクが正しくありません。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        password_reset_token = verify_password_reset_token(token)
        if not password_reset_token:
            logger.warning(f"パスワード再設定トークン検証失敗: token_length={len(token)}")
            # トークンが存在するか確認
            from .models import PasswordResetToken
            try:
                existing_token = PasswordResetToken.objects.get(token=token)
                logger.warning(f"トークンは存在するが無効: used_at={existing_token.used_at}, expires_at={existing_token.expires_at}, now={timezone.now()}")
            except PasswordResetToken.DoesNotExist:
                logger.warning(f"トークンが存在しない: token_length={len(token)}")

            return Response(
                {'error': 'このリンクは有効期限が切れているか、既に使用済みです。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        logger.info(f"パスワード再設定トークン検証成功: user_id={password_reset_token.user.id}")
        return Response({'valid': True}, status=status.HTTP_200_OK)
    except Exception as e:
        logger.error(f"パスワード再設定トークン検証エラー: error={str(e)}", exc_info=True)
        return Response(
            {'error': '予期しないエラーが発生しました。しばらく時間をおいて再度お試しください。'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(["POST"])
@permission_classes([AllowAny])
def reset_password(request: Request) -> Response:
    """パスワード再設定API"""
    try:
        token = request.data.get('token', '').strip()
        new_password = request.data.get('password', '')

        if not token:
            return Response(
                {'error': 'リンクが正しくありません。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        validation_error = validate_password_strength(new_password)
        if validation_error:
            return Response(
                {'error': validation_error},
                status=status.HTTP_400_BAD_REQUEST
            )

        password_reset_token = verify_password_reset_token(token)
        if not password_reset_token:
            return Response(
                {'error': 'このリンクは有効期限が切れているか、既に使用済みです。'},
                status=status.HTTP_400_BAD_REQUEST
            )

        user = password_reset_token.user
        user.set_password(new_password)
        user.save(update_fields=['password'])

        password_reset_token.mark_as_used()

        logger.info(f"パスワード再設定成功: user_id={user.id}")
        return Response(
            {'message': 'パスワードを再設定しました。ログインしてください。'},
            status=status.HTTP_200_OK
        )
    except Exception as e:
        logger.error(f"パスワード再設定エラー: error={str(e)}", exc_info=True)
        return Response(
            {'error': '予期しないエラーが発生しました。しばらく時間をおいて再度お試しください。'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
