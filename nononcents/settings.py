import ipaddress
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured


def env_bool(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(name)

    if raw_value is None:
        return default

    normalized = raw_value.strip().lower()

    if normalized in {"1", "true", "yes", "on"}:
        return True

    if normalized in {"0", "false", "no", "off"}:
        return False

    raise ValueError(f"{name} must be true/false, yes/no, on/off, or 1/0")


BASE_DIR = Path(__file__).resolve().parent.parent
_DEV_SECRET_KEY = "unsafe-development-only-key"
# The presence of POSTGRES_DB is the production switch (it also selects PostgreSQL).
IS_PRODUCTION = bool(os.environ.get("POSTGRES_DB"))
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "" if IS_PRODUCTION else _DEV_SECRET_KEY)
if IS_PRODUCTION and (not SECRET_KEY.strip() or SECRET_KEY == _DEV_SECRET_KEY):
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY must be set to a unique, non-default value in production."
    )
DEBUG = env_bool("DJANGO_DEBUG", not IS_PRODUCTION)
ALLOWED_HOSTS = [
    x for x in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",") if x
]
CSRF_TRUSTED_ORIGINS = [x for x in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if x]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "budget",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "budget.csp.ContentSecurityPolicyMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "budget.first_run.FirstRunMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "nononcents.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "budget.context_processors.backup_alert",
                "budget.context_processors.undo_delete",
                "budget.context_processors.page_names",
                "budget.context_processors.appearance",
            ]
        },
    }
]
WSGI_APPLICATION = "nononcents.wsgi.application"

if os.environ.get("POSTGRES_DB"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ["POSTGRES_DB"],
            "USER": os.environ["POSTGRES_USER"],
            "PASSWORD": os.environ["POSTGRES_PASSWORD"],
            "HOST": os.environ.get("POSTGRES_HOST", "db"),
            "PORT": "5432",
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.environ.get("SQLITE_PATH", str(BASE_DIR / "db.sqlite3")),
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]
LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
# Hashed file names (app.4f2c1e.css) so each deploy's CSS and JS load at once and can be
# cached for a year. STATICFILES_STORAGE was removed in Django 5.1 and was being ignored.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"
SESSION_COOKIE_AGE = int(os.environ.get("SESSION_COOKIE_AGE", "28800"))
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = env_bool(
    "SESSION_COOKIE_SECURE",
    False,
)
CSRF_COOKIE_SECURE = env_bool(
    "CSRF_COOKIE_SECURE",
    False,
)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SECURE_SSL_REDIRECT = env_bool(
    "SECURE_SSL_REDIRECT",
    False,
)
# HTTPS ends at a reverse proxy, which sends X-Forwarded-Proto. Trust it only behind that proxy
# (the app port is bound to the host's loopback address in compose.yml by default).
TRUST_PROXY_HTTPS = env_bool("TRUST_PROXY_HTTPS", False)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https") if TRUST_PROXY_HTTPS else None
if SECURE_SSL_REDIRECT and not TRUST_PROXY_HTTPS:
    raise ImproperlyConfigured(
        "SECURE_SSL_REDIRECT=true needs TRUST_PROXY_HTTPS=true behind a reverse proxy; "
        "otherwise every request redirects to itself."
    )
# The container health check calls http://127.0.0.1:8000/health/ directly.
SECURE_REDIRECT_EXEMPT = [r"^health/$"]
# Browsers remember to use HTTPS for this many seconds. Leave 0 until HTTPS is confirmed working.
SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", "0"))

# Shared with the host job scripts/backup-request.sh ("Back up now" in Admin).
BACKUP_REQUEST_DIR = Path(
    os.environ.get("BACKUP_REQUEST_DIR_IN_APP", "/var/lib/nononcents/backup-requests")
)
BACKUP_STATUS_FILE = Path(
    os.environ.get("BACKUP_STATUS_FILE", "/var/lib/nononcents/backup-status/backup-status.json")
)
BACKUP_MAX_AGE_HOURS = max(1, min(int(os.environ.get("BACKUP_MAX_AGE_HOURS", "26")), 720))
REPORT_LLM_PROVIDER = os.environ.get("REPORT_LLM_PROVIDER", "disabled")
REPORT_LLM_URL = os.environ.get("REPORT_LLM_URL", "")
REPORT_LLM_MODEL = os.environ.get("REPORT_LLM_MODEL", "")
# Optional bearer token for endpoints that need one. Never logged or shown.
REPORT_LLM_API_KEY = os.environ.get("REPORT_LLM_API_KEY", "")
REPORT_LLM_TIMEOUT_SECONDS = max(
    5, min(int(os.environ.get("REPORT_LLM_TIMEOUT_SECONDS", "120")), 300)
)
# Some local reasoning models think before they answer, which can be slow and can cut a long
# answer short. Turning reasoning off (where the server supports it) gives a complete, faster
# answer.
REPORT_LLM_DISABLE_THINKING = env_bool("REPORT_LLM_DISABLE_THINKING", False)
# Reasoning models spend part of this budget thinking before they answer.
REPORT_LLM_MAX_TOKENS = max(500, min(int(os.environ.get("REPORT_LLM_MAX_TOKENS", "2200")), 32000))


def _trusted_proxy_networks():
    """Networks the reverse proxy reaches the app from; only they may name the visitor (X-Real-IP).

    Default: loopback and the private ranges Docker uses for its bridge networks.
    """
    raw = os.environ.get("TRUSTED_PROXY_NETWORKS", "127.0.0.1/32,::1/128,172.16.0.0/12")
    networks = []
    for entry in (x.strip() for x in raw.split(",")):
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError as exc:
            raise ImproperlyConfigured(
                f"TRUSTED_PROXY_NETWORKS has an invalid network: {entry!r}"
            ) from exc
    return networks


TRUSTED_PROXY_NETWORKS = _trusted_proxy_networks()

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
            "formatter": "simple",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": os.environ.get("DJANGO_LOG_LEVEL", "WARNING").upper(),
    },
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "django.security": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "budget": {"handlers": ["console"], "level": "INFO", "propagate": False},
        # pypdf warns about statement fonts it cannot fully decode; the text is still read
        # and every row is checked against the printed balance, so keep only errors.
        "pypdf": {"handlers": ["console"], "level": "ERROR", "propagate": False},
    },
}
