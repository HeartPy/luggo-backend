from .settings import *

env_file = BASE_DIR / '.env.prod'

DEBUG = False

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

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