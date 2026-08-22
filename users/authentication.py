from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .account_access import is_account_blocked


class ActiveBusinessOwnerSessionAuthentication(SessionAuthentication):
    """無効な事業者・配達者はセッションがあっても未ログインとして扱う"""

    def authenticate(self, request):  # type: ignore[no-untyped-def]
        result = super().authenticate(request)
        if result is None:
            return None
        user, auth = result
        if is_account_blocked(user):
            return None
        return (user, auth)


class ActiveBusinessOwnerTokenAuthentication(TokenAuthentication):
    """無効な事業者・配達者のトークン認証を拒否"""

    def authenticate_credentials(self, key):  # type: ignore[no-untyped-def]
        user, token = super().authenticate_credentials(key)
        if is_account_blocked(user):
            raise AuthenticationFailed('User inactive or deleted.')
        return (user, token)
