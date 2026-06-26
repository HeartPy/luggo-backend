from django.utils import timezone
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from typing import Optional, Tuple
import secrets
import logging

from project.email import send_email, email_signature
from .models import PasswordResetToken


User = get_user_model()
logger = logging.getLogger(__name__)

def generate_login_verification_code(user: User) -> str:
    """ログイン用認証コードを生成（6桁の数字）"""
    code = ''.join([str(secrets.randbelow(10)) for _ in range(6)])
    expires_at = timezone.now() + timezone.timedelta(minutes=3)
    now = timezone.now()

    # セッション開始時刻が未設定の場合、または30分以上経過している場合は新規セッションとして開始
    update_fields = ['verification_code', 'verification_code_expires_at', 'verification_code_attempts']
    if not user.login_session_started_at or (now - user.login_session_started_at).total_seconds() > 30 * 60:
        user.login_session_started_at = now
        update_fields.append('login_session_started_at')

    user.verification_code = code
    user.verification_code_expires_at = expires_at
    user.verification_code_attempts = 0  # 新しい認証コード生成時に失敗回数をリセット
    user.save(update_fields=update_fields)

    logger.info(f"ログイン認証コード生成: email={user.email}")
    return code


def check_login_session_validity(user: User) -> bool:
    """
    ログイン試行全体の有効期限をチェック（30分）

    認証コード送信からログイン完了までの期間が30分以内かどうかを確認します。
    これはログイン成功後のセッション有効期限（1時間）とは異なります。
    """
    if not user.login_session_started_at:
        return False

    now = timezone.now()
    session_age = (now - user.login_session_started_at).total_seconds()

    # 30分を超えている場合は無効
    return session_age <= 30 * 60


def clear_login_session(user: User) -> None:
    """ログインセッションをクリア"""
    user.login_session_started_at = None
    user.save(update_fields=['login_session_started_at'])


def send_login_verification_code(user: User, code: str) -> bool:
    """ログイン認証コードをメールで送信"""
    subject = "【LugGo】ログイン認証コード"
    message = f"""
LugGo（ラグゴー）をご利用いただきありがとうございます。

ログインの確認のため、下記の認証コードをご入力ください。

認証コード：{code}

有効期限：3分

※この認証コードは第三者に共有しないでください。
※このメールにお心当たりがない場合は、破棄してください。

{email_signature()}
"""

    return send_email(subject=subject, text=message, to=user.email)


def verify_login_code(user: User, code: str) -> bool:
    """ログイン認証コードを検証"""
    if not user.verification_code or not user.verification_code_expires_at:
        return False

    if user.verification_code != code:
        return False

    if timezone.now() > user.verification_code_expires_at:
        # 有効期限切れの場合は認証コードをクリア
        user.verification_code = ''
        user.verification_code_expires_at = None
        user.save(update_fields=['verification_code', 'verification_code_expires_at'])
        return False

    return True


def clear_verification_code(user: User) -> None:
    """認証コードをクリア"""
    user.verification_code = ''
    user.verification_code_expires_at = None
    user.verification_code_attempts = 0
    user.save(update_fields=['verification_code', 'verification_code_expires_at', 'verification_code_attempts'])


def generate_password_reset_token(user: User) -> PasswordResetToken:
    """パスワード再設定用トークンを生成"""
    # 既存の有効なトークンがある場合は無効化
    existing_tokens = PasswordResetToken.objects.filter(
        user=user,
        used_at__isnull=True,
        expires_at__gt=timezone.now()
    )
    if existing_tokens.exists():
        existing_tokens.update(used_at=timezone.now())

    # 新しいトークンを生成
    token = secrets.token_urlsafe(32)
    expires_at = timezone.now() + timezone.timedelta(minutes=30)

    password_reset_token = PasswordResetToken.objects.create(
        user=user,
        token=token,
        expires_at=expires_at,
    )

    logger.info(f"パスワード再設定トークン生成: user_id={user.id}")
    return password_reset_token


def send_password_reset_email(user: User, token: str) -> bool:
    """パスワード再設定メールを送信"""
    subject = "【LugGo】パスワード再設定のご案内"
    reset_link = f"{settings.FRONTEND_BASE_URL}/account/password/reset?token={token}"
    message = f"""
LugGo（ラグゴー）をご利用いただきありがとうございます。

パスワード再設定のため、下記のリンクから手続きを進めてください。

{reset_link}

有効期限：30分

※このメールにお心当たりがない場合は、破棄してください。

{email_signature()}
"""

    return send_email(subject=subject, text=message, to=user.email)


def verify_password_reset_token(token: str) -> Optional[PasswordResetToken]:
    """パスワード再設定トークンを検証"""
    if not token:
        logger.warning("verify_password_reset_token: トークンが空")
        return None

    try:
        password_reset_token = PasswordResetToken.objects.get(token=token)
        logger.info(f"verify_password_reset_token: トークンが見つかりました: user_id={password_reset_token.user.id}, expires_at={password_reset_token.expires_at}, used_at={password_reset_token.used_at}")
    except PasswordResetToken.DoesNotExist:
        logger.warning(f"verify_password_reset_token: トークンが存在しません: token_length={len(token)}")
        return None

    if not password_reset_token.is_valid():
        logger.warning(f"verify_password_reset_token: トークンが無効です: used_at={password_reset_token.used_at}, expires_at={password_reset_token.expires_at}, now={timezone.now()}")
        return None

    logger.info(f"verify_password_reset_token: トークンが有効です: user_id={password_reset_token.user.id}")
    return password_reset_token


def get_client_ip(request) -> str:
    """クライアントのIPアドレスを取得"""
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0]
    else:
        ip = request.META.get('REMOTE_ADDR', '')
    return ip.strip()


def check_ip_login_attempts(ip_address: str) -> Tuple[bool, Optional[str]]:
    """IPアドレスベースのログイン試行回数制限をチェック"""
    if not ip_address:
        return False, None

    cache_key = f'login_attempts:{ip_address}'
    attempts_data = cache.get(cache_key)

    if attempts_data is None:
        return False, None

    attempts_count = attempts_data.get('count', 0)
    blocked_until = attempts_data.get('blocked_until')

    if blocked_until:
        now = timezone.now()
        if now < blocked_until:
            # まだブロック中
            remaining_minutes = int((blocked_until - now).total_seconds() / 60)
            return True, f'ログイン試行回数が上限に達しました。{remaining_minutes}分後に再度お試しください。'
        else:
            # ブロック期間が過ぎたのでクリア
            cache.delete(cache_key)
            return False, None

    return False, None


def record_login_failure(ip_address: str) -> None:
    """
    IPアドレスベースのログイン失敗を記録

    5分間に10回失敗したら、30分間ブロック
    """
    if not ip_address:
        return

    cache_key = f'login_attempts:{ip_address}'
    attempts_data = cache.get(cache_key)

    now = timezone.now()

    if attempts_data is None:
        attempts_data = {
            'count': 1,
            'first_attempt': now,
            'blocked_until': None,
        }
    else:
        # ブロック中でない場合のみカウントを増やす
        if not attempts_data.get('blocked_until') or now >= attempts_data['blocked_until']:
            attempts_data['count'] = attempts_data.get('count', 0) + 1

            # 5分間のウィンドウをチェック
            first_attempt = attempts_data.get('first_attempt', now)
            time_window = (now - first_attempt).total_seconds()

            if attempts_data['count'] >= 10 and time_window <= 5 * 60:  # 5分間
                # 30分間ブロック
                attempts_data['blocked_until'] = now + timezone.timedelta(minutes=30)
                logger.warning(f"IPアドレスがブロックされました: ip={ip_address}, attempts={attempts_data['count']}")
            elif time_window > 5 * 60:
                # 5分を超えたのでカウントをリセット
                attempts_data['count'] = 1
                attempts_data['first_attempt'] = now
        else:
            # ブロック中なので何もしない
            return

    # キャッシュに保存（ブロック中は30分、そうでない場合は5分）
    if attempts_data.get('blocked_until'):
        cache_timeout = int((attempts_data['blocked_until'] - now).total_seconds())
    else:
        cache_timeout = 5 * 60  # 5分

    cache.set(cache_key, attempts_data, cache_timeout)


def record_login_success(ip_address: str) -> None:
    """
    ログイン成功時にIPアドレスベースの失敗カウントをクリア
    """
    if not ip_address:
        return

    cache_key = f'login_attempts:{ip_address}'
    cache.delete(cache_key)
