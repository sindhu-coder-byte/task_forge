"""
Django settings for VetriFlow.
"""

import importlib
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / '.env')

# Allow PyMySQL to act as MySQLdb so Django's mysql backend works on cPanel/VPS.
# Safe no-op when PyMySQL is not installed (Render uses psycopg2 for PostgreSQL).
try:
    pymysql = importlib.import_module('pymysql')
    pymysql.install_as_MySQLdb()
except ImportError:
    pass


def _env(key, default=''):
    """Read an env var and strip surrounding quotes that dashboards sometimes add.

    Handles both  KEY="value"  and  KEY='value'  — the quotes become literal
    characters when pasted into Render / Heroku UI, breaking passwords and keys.
    Also strips leading/trailing whitespace.
    """
    val = (os.environ.get(key) or default or '').strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
        val = val[1:-1].strip()
    return val


def _should_use_sqlite_fallback(database_url, debug=False, force_mysql=False):
    """Use SQLite for local debug runs and Render-style builds when a MySQL URL is configured."""
    if force_mysql:
        return False
    if not database_url:
        return True
    if os.getenv('USE_SQLITE_FALLBACK', 'True').lower() not in {'1', 'true', 'yes', 'on'}:
        return False
    if debug:
        return database_url.startswith(('mysql://', 'mysql2://'))
    return os.getenv('RENDER') == 'true' and database_url.startswith(('mysql://', 'mysql2://'))


# ── CORE ────────────────────────────────────────────────────────────────────
SECRET_KEY = _env('SECRET_KEY', 'django-insecure-local-dev-change-me')

# "True" / "False" string from env; defaults to False in production
DEBUG = os.getenv('DEBUG', 'False') == 'True'

# Build ALLOWED_HOSTS: localhost + anything in env + Render's auto-injected hostname
_allowed_env = os.getenv('ALLOWED_HOSTS', '')
ALLOWED_HOSTS = ['localhost', '127.0.0.1']
if _allowed_env:
    ALLOWED_HOSTS += [h.strip() for h in _allowed_env.split(',') if h.strip()]

# Local dev only (DEBUG is never True in production — see the DEBUG assignment
# above): allow any host so the mobile_app Flutter build can reach `runserver`
# whether it's running in the Android emulator (10.0.2.2) or on a physical
# phone over the LAN (whatever IP DHCP hands the dev machine that day).
if DEBUG:
    ALLOWED_HOSTS.append('*')

# Render injects RENDER_EXTERNAL_HOSTNAME automatically — add it if present
_render_host = os.getenv('RENDER_EXTERNAL_HOSTNAME')
if _render_host:
    ALLOWED_HOSTS.append(_render_host)

# Add custom production domains
for host in ['vetriemsportal.com', 'www.vetriemsportal.com']:
    if host not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(host)

# ── APPS ─────────────────────────────────────────────────────────────────────
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.sites',

    'core.apps.CoreConfig',
    'channels',

    'allauth',
    'allauth.account',
    'allauth.socialaccount',
    'allauth.socialaccount.providers.google',

    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'corsheaders',
]

AUTHENTICATION_BACKENDS = [
    'django.contrib.auth.backends.ModelBackend',
    'allauth.account.auth_backends.AuthenticationBackend',
]

# ── MIDDLEWARE ────────────────────────────────────────────────────────────────
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'core.middleware.OAuthStateFallbackMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'allauth.account.middleware.AccountMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / "core" / "templates", BASE_DIR / "templates"],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'core.context_processors.global_user_context',
                'core.context_processors.invite_roles',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

# ── DATABASE ──────────────────────────────────────────────────────────────────
import dj_database_url

DATABASE_URL = os.environ.get('DATABASE_URL')

if _should_use_sqlite_fallback(DATABASE_URL, debug=DEBUG):
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }
elif DATABASE_URL:
    DATABASES = {
        'default': dj_database_url.config(
            # Managed hosts (especially free/trial-tier instances like Aiven)
            # can silently close idle connections well before 600s — a worker
            # then tries to reuse a dead connection and the request 500s.
            # A short conn_max_age means we reconnect far more often, trading
            # a little latency for not holding onto connections long enough
            # to go stale.
            default=DATABASE_URL,
            conn_max_age=60,
            conn_health_checks=True,
        )
    }
    # Add MySQL charset options when using MySQL/MariaDB
    if DATABASE_URL.startswith('mysql://') or DATABASE_URL.startswith('mysql2://'):
        DATABASES['default'].setdefault('OPTIONS', {})
        DATABASES['default']['OPTIONS']['charset'] = 'utf8mb4'
        DATABASES['default']['OPTIONS']['connect_timeout'] = 10

        # dj_database_url copies query-string params (e.g. ?ssl-mode=REQUIRED,
        # used by managed hosts like Aiven) straight into OPTIONS with their
        # literal hyphens, but MySQLdb/PyMySQL only accept them as underscored
        # kwargs — rename so TLS actually gets negotiated instead of silently
        # being dropped as an unrecognized option.
        for key in list(DATABASES['default']['OPTIONS']):
            if '-' in key:
                DATABASES['default']['OPTIONS'][key.replace('-', '_')] = DATABASES['default']['OPTIONS'].pop(key)
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }

# ── CHANNELS ──────────────────────────────────────────────────────────────────
REDIS_URL = os.environ.get('REDIS_URL')

if REDIS_URL:
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels_redis.core.RedisChannelLayer',
            'CONFIG': {'hosts': [REDIS_URL]},
        }
    }
else:
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels.layers.InMemoryChannelLayer',
        }
    }

# ── PASSWORD VALIDATION ───────────────────────────────────────────────────────
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# ── I18N ──────────────────────────────────────────────────────────────────────
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# ── STATIC FILES ──────────────────────────────────────────────────────────────
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# Only include the project-level static dir if it actually exists
_project_static = BASE_DIR / 'static'
STATICFILES_DIRS = [
    BASE_DIR / 'core' / 'static',  # Include app static files explicitly
]
if _project_static.is_dir():
    STATICFILES_DIRS.append(_project_static)

# WhiteNoise: compressed + cached static files for production
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'
WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = DEBUG
# ── MEDIA ─────────────────────────────────────────────────────────────────────
MEDIA_URL = '/task_files/'
MEDIA_ROOT = BASE_DIR / 'task_files'

WHITENOISE_USE_FINDERS = True

# ── SESSION ───────────────────────────────────────────────────────────────────
# Local dev: store session in a signed cookie so OAuth state survives the
# cross-site redirect from Google back to 127.0.0.1 (Chrome drops the
# DB-session cookie on that redirect in some versions).
# Production: keep the default DB backend.
if DEBUG:
    SESSION_ENGINE = 'django.contrib.sessions.backends.signed_cookies'
    SESSION_SAVE_EVERY_REQUEST = True
else:
    SESSION_ENGINE = 'django.contrib.sessions.backends.db'

SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_HTTPONLY = True

# ── SECURITY (production only) ────────────────────────────────────────────────
if not DEBUG:
    # Render terminates TLS at its proxy — trust the forwarded proto header
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000          # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True

# ── CSRF ──────────────────────────────────────────────────────────────────────
CSRF_TRUSTED_ORIGINS = [
    'https://*.onrender.com',
    'https://vetriemsportal.com',
    'https://www.vetriemsportal.com',
]

_site_url = os.environ.get('SITE_URL', '').rstrip('/')
if _site_url and _site_url not in CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS.append(_site_url)

# ── AUTH ──────────────────────────────────────────────────────────────────────
SITE_ID = int(os.environ.get('SITE_ID', '1'))

LOGIN_URL = 'core:login'
LOGIN_REDIRECT_URL = '/role-redirect/'
LOGOUT_REDIRECT_URL = 'core:home'

ACCOUNT_LOGIN_METHODS = {'username', 'email'}
ACCOUNT_SIGNUP_FIELDS = ['email*', 'username*', 'password1*', 'password2*']
ACCOUNT_EMAIL_VERIFICATION = 'none'
ACCOUNT_LOGOUT_ON_GET = True
# OAuth callback URLs must use https:// in production
ACCOUNT_DEFAULT_HTTP_PROTOCOL = 'https' if not DEBUG else 'http'

GOOGLE_OAUTH2_CLIENT_ID     = _env('GOOGLE_OAUTH2_CLIENT_ID')
GOOGLE_OAUTH2_CLIENT_SECRET = _env('GOOGLE_OAUTH2_CLIENT_SECRET')

_google_app = {}
if GOOGLE_OAUTH2_CLIENT_ID and GOOGLE_OAUTH2_CLIENT_SECRET:
    _google_app = {
        'APP': {
            'client_id': GOOGLE_OAUTH2_CLIENT_ID,
            'secret': GOOGLE_OAUTH2_CLIENT_SECRET,
            'key': '',
        }
    }

SOCIALACCOUNT_PROVIDERS = {
    'google': {
        **_google_app,
        'SCOPE': ['profile', 'email'],
        'AUTH_PARAMS': {'access_type': 'online'},
    }
}

SOCIALACCOUNT_LOGIN_ON_GET = True
SOCIALACCOUNT_ADAPTER = 'core.adapters.CustomSocialAccountAdapter'
SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True

# ── EMAIL ─────────────────────────────────────────────────────────────────────
EMAIL_HOST_USER     = _env('EMAIL_HOST_USER') or None
EMAIL_HOST_PASSWORD = _env('EMAIL_HOST_PASSWORD') or None

EMAIL_HOST    = _env('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT    = int(_env('EMAIL_PORT', '587'))
EMAIL_USE_TLS = _env('EMAIL_USE_TLS', 'True').lower() not in ('false', '0', 'no')
EMAIL_USE_SSL = _env('EMAIL_USE_SSL', 'False').lower() in ('true', '1', 'yes')

# ── Backend selection ────────────────────────────────────────────────────────
# Priority order:
#   1. BREVO_API_KEY set  → BrevoAPIBackend (HTTPS, any recipient, no domain needed)
#   2. EMAIL_HOST_USER=resend → ResendAPIBackend (HTTPS, only to account email without domain)
#   3. SMTP credentials set   → standard SMTP (Render free tier blocks 465/587)
#   4. fallback               → console (development)

BREVO_API_KEY = _env('BREVO_API_KEY') or None

if BREVO_API_KEY:
    EMAIL_BACKEND = 'core.email_backends.BrevoAPIBackend'
elif EMAIL_HOST_USER == 'resend' and EMAIL_HOST_PASSWORD:
    # Render free tier blocks ports 25/465/587; non-standard ports (2465/2587) work.
    _RENDER_BLOCKED_SMTP = {25, 465, 587}
    if EMAIL_PORT not in _RENDER_BLOCKED_SMTP:
        EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
        EMAIL_TIMEOUT = 30
    else:
        EMAIL_BACKEND = 'core.email_backends.ResendAPIBackend'
elif EMAIL_HOST_USER and EMAIL_HOST_PASSWORD:
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
    EMAIL_TIMEOUT = 30
else:
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

DEFAULT_FROM_EMAIL = _env('DEFAULT_FROM_EMAIL', EMAIL_HOST_USER or 'noreply@VetriFlow.app')

# ── MISC ──────────────────────────────────────────────────────────────────────
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Public URL used to build absolute links in emails (no trailing slash)
SITE_URL = os.environ.get('SITE_URL', 'http://127.0.0.1:8000').rstrip('/')

# Comma-separated admin emails — these users get admin privileges in the UI
TASKFORGE_ADMIN_EMAILS = [
    e.strip().lower()
    for e in os.environ.get('TASKFORGE_ADMIN_EMAILS', '').split(',')
    if e.strip()
]

# ── REST API (native Flutter app) ────────────────────────────────────────────
# The Flutter app is a native HTTP client, not a browser — CORS mostly matters
# for local web-based debugging (Postman-style tools honor CORS in a browser
# tab), so this stays narrow and credential-less; auth is header-based JWT.
CORS_ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get('CORS_ALLOWED_ORIGINS', '').split(',') if o.strip()
]
CORS_ALLOW_CREDENTIALS = False

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
}

from datetime import timedelta

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=45),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=14),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'AUTH_HEADER_TYPES': ('Bearer',),
}
