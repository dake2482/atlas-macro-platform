from __future__ import annotations

import os
from pathlib import Path

import dj_database_url
from celery.schedules import crontab

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-only-change-me")
DEBUG = os.getenv("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",")
    if host.strip()
]
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.postgres",
    "django.contrib.staticfiles",
    "research.apps.ResearchConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "atlasmacro.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "research.context_processors.site_context",
            ],
        },
    }
]

WSGI_APPLICATION = "atlasmacro.wsgi.application"
ASGI_APPLICATION = "atlasmacro.asgi.application"

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL:
    DATABASES = {"default": dj_database_url.parse(DATABASE_URL, conn_max_age=60)}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
RAW_ARTIFACT_ROOT = Path(os.getenv("RAW_ARTIFACT_ROOT", str(BASE_DIR / "data" / "artifacts")))
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
APPEND_SLASH = True
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
X_FRAME_OPTIONS = "DENY"
SITE_NAME = os.getenv("SITE_NAME", "Atlas Macro")
SITE_URL = os.getenv("SITE_URL", "http://localhost:8000").rstrip("/")
GITHUB_REPOSITORIES = os.getenv("GITHUB_REPOSITORIES", "")
NEWS_RSS_FEEDS = os.getenv("NEWS_RSS_FEEDS", "")

if not DEBUG:
    SECURE_SSL_REDIRECT = os.getenv("DJANGO_SECURE_SSL_REDIRECT", "1") == "1"
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.getenv("DJANGO_SECURE_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", CELERY_BROKER_URL)
CELERY_TASK_ALWAYS_EAGER = os.getenv("CELERY_TASK_ALWAYS_EAGER", "0") == "1"
CELERY_TIMEZONE = TIME_ZONE
CELERY_BEAT_SCHEDULE = {
    "refresh-news-hourly": {
        "task": "research.tasks.refresh_news_sources",
        "schedule": crontab(minute=12),
    },
    "refresh-berkshire-letters-weekly": {
        "task": "research.tasks.refresh_berkshire_letter_sources",
        "schedule": crontab(day_of_week="mon", hour=9, minute=40),
    },
    "refresh-official-every-2h": {
        "task": "research.tasks.refresh_official_sources",
        "schedule": crontab(hour="*/2", minute=22),
    },
    "refresh-h41-weekly": {
        "task": "research.tasks.refresh_h41_sources",
        "schedule": crontab(day_of_week="fri", hour=6, minute=0),
    },
    "refresh-h8-weekly": {
        "task": "research.tasks.refresh_h8_sources",
        "schedule": crontab(day_of_week="sat", hour=6, minute=20),
    },
    "refresh-prates-daily": {
        "task": "research.tasks.refresh_prates_sources",
        "schedule": crontab(hour=6, minute=20),
    },
    "refresh-h10-daily": {
        "task": "research.tasks.refresh_h10_sources",
        "schedule": crontab(hour=6, minute=40),
    },
    "refresh-treasury-curves-daily": {
        "task": "research.tasks.refresh_treasury_curve_sources",
        "schedule": crontab(hour=7, minute=5),
    },
    "refresh-credit-official-daily": {
        "task": "research.tasks.refresh_credit_official_sources",
        "schedule": crontab(hour=11, minute=10),
    },
    "refresh-macro-official-daily": {
        "task": "research.tasks.refresh_macro_official_sources",
        "schedule": crontab(hour=12, minute=10),
    },
    "refresh-market-daily": {
        "task": "research.tasks.refresh_market_sources",
        "schedule": crontab(hour=7, minute=20),
    },
    "refresh-filings-hourly": {
        "task": "research.tasks.refresh_filing_sources",
        "schedule": crontab(minute=32),
    },
    "refresh-cftc-weekly": {
        "task": "research.tasks.refresh_cftc_sources",
        "schedule": crontab(day_of_week="sat", hour=8, minute=0),
    },
    "refresh-cftc-holiday-recheck": {
        # CFTC notes that federal holidays can move the usual Friday release
        # to Monday. Tuesday morning China time catches that delayed batch.
        "task": "research.tasks.refresh_cftc_sources",
        "schedule": crontab(day_of_week="tue", hour=8, minute=0),
    },
    "refresh-github-daily": {
        "task": "research.tasks.refresh_github_sources",
        "schedule": crontab(hour=10, minute=20),
    },
    "publish-daily-evidence-every-2h": {
        "task": "research.tasks.publish_daily_evidence",
        "schedule": crontab(hour="*/2", minute=40),
    },
    "generate-daily-research-every-2h": {
        "task": "research.tasks.generate_daily_research",
        "schedule": crontab(hour="*/2", minute=45),
    },
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": os.getenv("LOG_LEVEL", "INFO")},
    # httpx's INFO record includes the complete request URL. Some official APIs
    # accept credentials only as query parameters, so request logging must not
    # turn the worker log into a secret store.
    "loggers": {
        "httpx": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "httpcore": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}
