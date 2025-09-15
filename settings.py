import os
from pathlib import Path
import sys

import environ

BASE_DIR = Path(__file__).resolve().parent.parent
environ.Env.read_env(os.path.join(BASE_DIR, '.env'))

# 환경변수에서 SECRET_KEY 가져오기 (기본값은 개발용)
SECRET_KEY = os.environ.get('SECRET_KEY', 'django-insecure-7s5m0#5cfyxg8cf=2shff(1g)&1izp+*^jez_&s!z*g93l*h51')

ALLOWED_HOSTS = ['*']
DEBUG = True

CSRF_TRUSTED_ORIGINS = [
    "http://127.0.0.1:7999",  # 로컬 개발 환경
    "http://localhost:7999",
    "http://0.0.0.0:7999",  # Docker 환경에서 실행 시
]

CSRF_USE_SESSIONS = True

ENV_GENERAL = environ

# 로깅 설정 개선
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {process:d} {thread:d} {message}",
            "style": "{",
        },
        "simple": {
            "format": "{levelname} {message}",
            "style": "{",
        },
        "detailed": {
            "format": "{levelname} {asctime} {name} {funcName}:{lineno} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "detailed",
        },
        "file": {
            "class": "logging.FileHandler",
            "filename": os.path.join(BASE_DIR, "logs", "django.log"),
            "formatter": "verbose",
        },
        "error_file": {
            "class": "logging.FileHandler",
            "filename": os.path.join(BASE_DIR, "logs", "error.log"),
            "formatter": "verbose",
            "level": "ERROR",
        },
        "performance_file": {
            "class": "logging.FileHandler",
            "filename": os.path.join(BASE_DIR, "logs", "performance.log"),
            "formatter": "detailed",
        },
    },
    "root": {
        "handlers": ["console", "file"],
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": ["console", "file"],
            "level": "INFO",
            "propagate": False,
        },
        "django.db.backends": {
            "handlers": ["console"],
            "level": "WARNING",  # DB 쿼리 로그는 WARNING만
            "propagate": False,
        },
        "django.request": {
            "handlers": ["error_file"],
            "level": "ERROR",
            "propagate": False,
        },
        "inventory2": {
            "handlers": ["console", "file", "performance_file"],
            "level": "INFO",
            "propagate": False,
        },
        "core": {
            "handlers": ["console", "file"],
            "level": "INFO",
            "propagate": False,
        },
        "performance": {
            "handlers": ["performance_file"],
            "level": "INFO",
            "propagate": False,
        },
    },
}

# 로그 디렉토리 생성
log_dir = os.path.join(BASE_DIR, "logs")
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'inventory2.apps.Inventory2Config',
    'rest_framework',
    'drf_yasg',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'core.monitoring.PerformanceMonitoringMiddleware',  # 성능 모니터링 미들웨어
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [os.path.join(BASE_DIR, 'templates')],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
            'libraries': {
                'custom_filters': 'core.custom_filters',
            }
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# 환경변수에서 데이터베이스 설정 가져오기
DB_ENGINE = os.environ.get("DB_ENGINE", "django.db.backends.postgresql_psycopg2")
DB_NAME = os.environ.get("DB_NAME", "inventory-db")
DB_USER = os.environ.get("DB_USER", "LocalMaster")
DB_HOST = os.environ.get("DB_HOST", "db")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "q1w2e3!!")
DB_PORT = os.environ.get("DB_PORT", "5432")

# 테스트 환경에서는 SQLite 사용
if 'test' in sys.argv or os.environ.get('TESTING') == 'true':
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': ':memory:',
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': DB_ENGINE,
            'NAME': DB_NAME,
            'USER': DB_USER,
            'HOST': DB_HOST,
            'PASSWORD': DB_PASSWORD,
            'PORT': DB_PORT,
            'OPTIONS': {
                'connect_timeout': 10,
            },
            'CONN_MAX_AGE': 60,  # 연결 풀링
        }
    }

# 캐시 설정
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'unique-snowflake',
        'TIMEOUT': 300,  # 5분
        'OPTIONS': {
            'MAX_ENTRIES': 1000,
        }
    }
}

# 성능 모니터링 설정
CACHE_TIMEOUT = 300
PERFORMANCE_MONITORING = True
SLOW_QUERY_THRESHOLD = 1.0  # 1초 이상 쿼리 로깅

REST_FRAMEWORK = {
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 40,
    'DEFAULT_THROTTLE_CLASSES': [],
    'DEFAULT_THROTTLE_RATES': {},
}

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

LANGUAGE_CODE = 'ko-kr'

TIME_ZONE = 'Asia/Seoul'

APPEND_SLASH = False

USE_I18N = True

USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = os.path.join(BASE_DIR, "staticfiles")
STATICFILES_DIRS = (os.path.join(BASE_DIR, "static"),)
MEDIA_ROOT = os.path.join(BASE_DIR, "media")
MEDIA_URL = '/media/'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# 환경변수에서 API URL 가져오기
INVENTORY_API_BASE_URL = os.environ.get("INVENTORY_API_BASE_URL", "http://210.183.209.152:7999/api/")
INVENTORY_BASE_URL = os.environ.get("INVENTORY_BASE_URL", "http://210.183.209.152:7999/")
