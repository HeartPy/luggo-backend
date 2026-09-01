from .settings import *
from .tenant_origins import TENANT_SUBDOMAIN_ORIGIN_REGEX

env_file = BASE_DIR / '.env.production'

DEBUG = False

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# luggo.delivery 配下のサブドメイン間でセッション・CSRF Cookie を共有
SESSION_COOKIE_DOMAIN = ".luggo.delivery"
CSRF_COOKIE_DOMAIN = ".luggo.delivery"

# 事業者サブドメイン（acme.luggo.delivery 等）から api.luggo.delivery への API 呼び出しを許可。
# CORS_ALLOWED_ORIGINS / CSRF_TRUSTED_ORIGINS（.env）は luggo.delivery と www のみ。
# 動的に増える事業者サブドメインはここで許可する（3〜12 文字の英小文字）。
CORS_ALLOWED_ORIGIN_REGEXES = [
    TENANT_SUBDOMAIN_ORIGIN_REGEX,
]
CSRF_TRUSTED_ORIGINS = [
    *CSRF_TRUSTED_ORIGINS,
    "https://*.luggo.delivery",
]

# Sentry（エラー監視）
# SENTRY_DSN が設定されているときだけ有効化する。
# 未設定なら何もしないため、ローカルやステージングには影響しない。
SENTRY_DSN = config('SENTRY_DSN', default='')
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[
            DjangoIntegration(),
            CeleryIntegration(),
        ],
        environment='production',
        # エラー監視のみ（パフォーマンス計測は使わない）
        traces_sample_rate=0,
        # 顧客のメールアドレス等の個人情報を Sentry に送らない
        send_default_pii=False,
    )

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'standard': {
            'format': "[%(asctime)s] %(levelname)s [%(name)s:%(lineno)s] %(message)s",
            'datefmt': "%d/%b/%Y %H:%M:%S"

        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'standard',
        },
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': 'WARNING',
        },
        'users': {
            'handlers': ['console'],
            'level': 'WARNING',
            'propagate': False,
        },
        'business_owners': {
            'handlers': ['console'],
            'level': 'WARNING',
            'propagate': False,
        },
        'bookings': {
            'handlers': ['console'],
            'level': 'WARNING',
            'propagate': False,
        },
    },
}