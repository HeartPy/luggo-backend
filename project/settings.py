"""
このファイルは開発環境用の設定です。
本番環境では project.prod_settings が使用されます。
project.prod_settings はこのファイルの設定を継承し、本番用設定で上書きします。
"""

from pathlib import Path
from decouple import Config, RepositoryEnv
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent


env_file = BASE_DIR / '.env.development'
config = Config(RepositoryEnv(str(env_file)))

SECRET_KEY = config('SECRET_KEY')
DEBUG = True

ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost,127.0.0.1', cast=lambda value: [item.strip() for item in value.split(',')])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    "users.apps.UsersConfig",
    "business_owners.apps.BusinessOwnersConfig",
    "bookings.apps.BookingsConfig",
    "drivers.apps.DriversConfig",
    "routing.apps.RoutingConfig",
    "administrators.apps.AdministratorsConfig",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "project.middleware.SlidingSessionMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "project.urls"

APPEND_SLASH = False

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / 'templates'],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "project.wsgi.application"

DATABASES = {
    'default': dj_database_url.config(
        default=config('DATABASE_URL', default='sqlite:///db.sqlite3')
    )
}

AUTH_USER_MODEL = 'users.User'

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

LANGUAGE_CODE = 'ja'
TIME_ZONE = 'Asia/Tokyo'
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

CORS_ALLOWED_ORIGINS = config(
    'CORS_ALLOWED_ORIGINS',
    default='http://localhost:3000,http://127.0.0.1:3000',
    cast=lambda value: [item.strip() for item in value.split(',')]
)

CORS_ALLOW_CREDENTIALS = True

# CSV ダウンロード時にフロントエンドがファイル名を読めるよう公開する
CORS_EXPOSE_HEADERS = ['Content-Disposition']

CSRF_TRUSTED_ORIGINS = config(
    'CSRF_TRUSTED_ORIGINS',
    default='http://localhost:3000,http://127.0.0.1:3000',
    cast=lambda value: [item.strip() for item in value.split(',')]
)

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',
        'rest_framework.authentication.TokenAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 10,
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
    'DEFAULT_PARSER_CLASSES': [
        'rest_framework.parsers.JSONParser',
        'rest_framework.parsers.MultiPartParser',
    ],
}

# メール送信設定
EMAIL_BACKEND = config('EMAIL_BACKEND', default='django.core.mail.backends.console.EmailBackend')
EMAIL_HOST = config('EMAIL_HOST', default='')
EMAIL_PORT = config('EMAIL_PORT', default=587, cast=int)
EMAIL_USE_TLS = config('EMAIL_USE_TLS', default=True, cast=bool)
EMAIL_HOST_USER = config('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')
DEFAULT_FROM_EMAIL = config('DEFAULT_FROM_EMAIL', default='noreply@luggo.com')

# ファイルアップロード設定
LUGGO_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
LUGGO_ALLOWED_FILE_TYPES = ['jpg', 'jpeg', 'png']

# セキュリティヘッダー設定
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'

# セッションクッキー設定
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = DEBUG
CSRF_COOKIE_SECURE = DEBUG

# SameSite属性設定
CSRF_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_SAMESITE = 'Lax'

# セッションタイムアウト設定
SESSION_COOKIE_AGE = 60 * 60  # 1時間（ログインセッション用・最終アクセスから）
DRIVER_SESSION_COOKIE_AGE = 60 * 60 * 24 * 7  # 1週間（配達者ログインセッション用・最終アクセスから）
TEMPORARY_SESSION_COOKIE_AGE = 30 * 60  # 30分（一時セッション用：Stripe設定、予約フロー）
SESSION_SAVE_EVERY_REQUEST = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = False

# Stripe設定
STRIPE_SECRET_KEY = config('STRIPE_SECRET_KEY', default='')
STRIPE_WEBHOOK_SECRET = config('STRIPE_WEBHOOK_SECRET', default='')

# Stripe Webhook 設定
STRIPE_WEBHOOK_BOOKING_GRACE_SECONDS = config(
    'STRIPE_WEBHOOK_BOOKING_GRACE_SECONDS', default=10, cast=int
)

# Resend 設定
RESEND_API_KEY = config('RESEND_API_KEY', default='')
RESEND_FROM_EMAIL = config('RESEND_FROM_EMAIL', default=DEFAULT_FROM_EMAIL)

# 運営への通知先メールアドレス（カンマ区切りで複数指定可）
OPERATIONS_NOTIFICATION_EMAIL = config(
    'OPERATIONS_NOTIFICATION_EMAIL',
    default='',
    cast=lambda value: [item.strip() for item in value.split(',') if item.strip()],
)

# 事業者向け手数料請求書の発行者情報
PLATFORM_INVOICE_ISSUER_NAME = config(
    'PLATFORM_INVOICE_ISSUER_NAME',
    default='LugGo（ラグゴー）運営事務局',
)
PLATFORM_INVOICE_ISSUER_ADDRESS = config(
    'PLATFORM_INVOICE_ISSUER_ADDRESS',
    default='',
)
PLATFORM_INVOICE_REGISTRATION_NUMBER = config(
    'PLATFORM_INVOICE_REGISTRATION_NUMBER',
    default='',
)
PLATFORM_INVOICE_SEAL_PATH = config(
    'PLATFORM_INVOICE_SEAL_PATH',
    default=str(
        BASE_DIR / 'business_owners' / 'assets' / 'platform_seal.png'
    ),
)

# フロントエンドのベースURL
FRONTEND_BASE_URL = config('FRONTEND_BASE_URL', default='http://localhost:3000')

# Google Places API設定
GOOGLE_PLACES_API_KEY = config('GOOGLE_PLACES_API_KEY', default='')
# Google Geocoding API設定
GOOGLE_GEOCODING_API_KEY = config('GOOGLE_GEOCODING_API_KEY', default='')
# Google Routes API設定
GOOGLE_ROUTES_API_KEY = config(
    'GOOGLE_ROUTES_API_KEY', default=GOOGLE_PLACES_API_KEY
)

# 日次自動割当
CELERY_BROKER_URL = config('CELERY_BROKER_URL', default='redis://redis:6379/0')
CELERY_RESULT_BACKEND = config(
    'CELERY_RESULT_BACKEND', default='redis://redis:6379/1'
)
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = config('CELERY_TASK_TIME_LIMIT', default=600, cast=int)

# 1回の自動割当で扱う上限
ROUTING_MAX_BOOKINGS = config(
    'ROUTING_MAX_BOOKINGS', default=1000, cast=int
)
ROUTING_MAX_TASKS = config('ROUTING_MAX_TASKS', default=2000, cast=int)
ROUTING_MAX_DRIVERS = config('ROUTING_MAX_DRIVERS', default=200, cast=int)
ROUTING_MAX_ADVANCE_DAYS = config(
    'ROUTING_MAX_ADVANCE_DAYS', default=90, cast=int
)

# 担当自動割当の計算設定（直線距離の概算で誰に振るかを決める）
ROUTING_ASSIGNMENT_TIME_LIMIT_SECONDS = config(
    'ROUTING_ASSIGNMENT_TIME_LIMIT_SECONDS', default=60, cast=int
)
ROUTING_ASSIGNMENT_DISTANCE_WEIGHT = config(
    'ROUTING_ASSIGNMENT_DISTANCE_WEIGHT', default=1, cast=int
)
ROUTING_ASSIGNMENT_BALANCE_PENALTY = config(
    'ROUTING_ASSIGNMENT_BALANCE_PENALTY', default=10000, cast=int
)
ROUTING_ASSIGNMENT_UNASSIGNED_PENALTY = config(
    'ROUTING_ASSIGNMENT_UNASSIGNED_PENALTY', default=1000000000, cast=int
)

# 将来: 担当が決まったタスクについて、配達者ごとの訪問ルート（順序・所要時間）を
# Google Routes の距離行列 + ルート探索で作るときに使う設定
ROUTING_CACHE_URL = config('ROUTING_CACHE_URL', default='')
if ROUTING_CACHE_URL:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            'LOCATION': ROUTING_CACHE_URL,
        }
    }
# 距離行列取得時の渋滞考慮度
ROUTING_GOOGLE_ROUTING_PREFERENCE = config(
    'ROUTING_GOOGLE_ROUTING_PREFERENCE', default='TRAFFIC_AWARE'
)
# 距離行列APIへ一度に送る地点数の上限
ROUTING_MATRIX_CHUNK_SIZE = config(
    'ROUTING_MATRIX_CHUNK_SIZE', default=25, cast=int
)
# 同じチャンクでの再試行回数
ROUTING_MATRIX_CHUNK_RETRIES = config(
    'ROUTING_MATRIX_CHUNK_RETRIES', default=2, cast=int
)
# 1分あたりに取得してよい距離行列要素数の上限（APIレート制限対策）
ROUTING_MATRIX_ELEMENTS_PER_MINUTE = config(
    'ROUTING_MATRIX_ELEMENTS_PER_MINUTE', default=3000, cast=int
)
# 距離行列の結果をキャッシュする秒数
ROUTING_MATRIX_CACHE_SECONDS = config(
    'ROUTING_MATRIX_CACHE_SECONDS', default=300, cast=int
)
# Routes API への HTTP タイムアウト（秒）
ROUTING_HTTP_TIMEOUT_SECONDS = config(
    'ROUTING_HTTP_TIMEOUT_SECONDS', default=30, cast=int
)
# 配達者1人分の訪問順を求める計算の最大秒数
ROUTING_SOLVER_TIME_LIMIT_SECONDS = config(
    'ROUTING_SOLVER_TIME_LIMIT_SECONDS', default=10, cast=int
)
# 各訪問先での作業時間の想定（秒）
ROUTING_SERVICE_SECONDS_PER_STOP = config(
    'ROUTING_SERVICE_SECONDS_PER_STOP', default=300, cast=int
)

# ロギング設定
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
        'users': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
        'business_owners': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
        'bookings': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}