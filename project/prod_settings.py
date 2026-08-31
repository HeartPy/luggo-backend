from .settings import *

env_file = BASE_DIR / '.env.production'

DEBUG = False

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

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