import os
from pathlib import Path
from urllib.parse import urlparse

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name, default=False):
    return os.getenv(name, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


def env_list(name, default=""):
    return [value.strip() for value in os.getenv(name, default).split(",") if value.strip()]


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "unsafe-development-key-change-me")
TOKEN_ENCRYPTION_KEY = os.getenv("NORTHBOUND_TOKEN_ENCRYPTION_KEY", "")
HARDCOVER_GRAPHQL_URL = os.getenv("HARDCOVER_GRAPHQL_URL", "https://api.hardcover.app/v1/graphql")
DEBUG = env_bool("DJANGO_DEBUG", True)

if not DEBUG and (SECRET_KEY == "unsafe-development-key-change-me" or SECRET_KEY.startswith("replace-")):
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG=0.")

NORTHBOUND_URL = os.getenv("NORTHBOUND_URL", "").strip().rstrip("/")
public_url = urlparse(NORTHBOUND_URL) if NORTHBOUND_URL else None
if public_url and (
    public_url.scheme not in {"http", "https"}
    or not public_url.hostname
    or public_url.username
    or public_url.password
    or public_url.path not in {"", "/"}
    or public_url.params
    or public_url.query
    or public_url.fragment
):
    raise ImproperlyConfigured("NORTHBOUND_URL must be a complete http:// or https:// URL.")

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],testserver")
if public_url and public_url.hostname not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(public_url.hostname)

CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")
if public_url:
    public_origin = f"{public_url.scheme}://{public_url.netloc}"
    if public_origin not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(public_origin)

NORTHBOUND_TRUST_PROXY_HEADERS = env_bool("NORTHBOUND_TRUST_PROXY_HEADERS")
if NORTHBOUND_TRUST_PROXY_HEADERS:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    USE_X_FORWARDED_HOST = True

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "core.middleware.RequestSizeLimitMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "core.middleware.RequirePasswordChangeMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "reading_challenge.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
WSGI_APPLICATION = "reading_challenge.wsgi.application"

database_url = os.getenv("DATABASE_URL", "").strip()
if database_url:
    database_config = dj_database_url.parse(
        database_url,
        conn_max_age=int(os.getenv("DATABASE_CONN_MAX_AGE", "60")),
        conn_health_checks=True,
    )
elif os.getenv("POSTGRES_HOST"):
    postgres_password = os.getenv("POSTGRES_PASSWORD", "")
    if not DEBUG and (not postgres_password or postgres_password.startswith("replace-")):
        raise ImproperlyConfigured("POSTGRES_PASSWORD must be set when using PostgreSQL in production.")
    database_config = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("POSTGRES_DB", "northbound"),
        "USER": os.getenv("POSTGRES_USER", "northbound"),
        "PASSWORD": postgres_password,
        "HOST": os.environ["POSTGRES_HOST"],
        "PORT": os.getenv("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": int(os.getenv("DATABASE_CONN_MAX_AGE", "60")),
        "CONN_HEALTH_CHECKS": True,
    }
else:
    database_config = dj_database_url.parse(f"sqlite:///{BASE_DIR / 'db.sqlite3'}")

DATABASES = {"default": database_config}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.getenv("TIME_ZONE", "America/New_York")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}
MEDIA_URL = os.getenv("NORTHBOUND_MEDIA_URL", "/media/")
if not MEDIA_URL.startswith("/"):
    MEDIA_URL = f"/{MEDIA_URL}"
if not MEDIA_URL.endswith("/"):
    MEDIA_URL = f"{MEDIA_URL}/"
MEDIA_ROOT = Path(os.getenv("NORTHBOUND_MEDIA_ROOT", BASE_DIR / "media"))
NORTHBOUND_SERVE_MEDIA = env_bool("NORTHBOUND_SERVE_MEDIA", DEBUG)
NORTHBOUND_MAX_PROFILE_PICTURE_BYTES = int(os.getenv("NORTHBOUND_MAX_PROFILE_PICTURE_BYTES", str(10 * 1024 * 1024)))
NORTHBOUND_MAX_REQUEST_BYTES = int(os.getenv("NORTHBOUND_MAX_REQUEST_BYTES", str(11 * 1024 * 1024)))
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

if not DEBUG:
    public_uses_https = bool(public_url and public_url.scheme == "https")
    SESSION_COOKIE_SECURE = env_bool("DJANGO_SESSION_COOKIE_SECURE", public_uses_https)
    CSRF_COOKIE_SECURE = env_bool("DJANGO_CSRF_COOKIE_SECURE", public_uses_https)
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT")
    SECURE_HSTS_SECONDS = int(os.getenv("DJANGO_SECURE_HSTS_SECONDS", "0"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS")
    SECURE_HSTS_PRELOAD = env_bool("DJANGO_SECURE_HSTS_PRELOAD")
