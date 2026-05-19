"""
Django settings for TaskForge.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / '.env')


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


# ── CORE ────────────────────────────────────────────────────────────────────
SECRET_KEY = _env('SECRET_KEY', 'django-insecure-k7x%c$_i_s1&0@8&%!i%6_=gbm5qvbqrrk#p#s5rvb1yg=hos8')

# "True" / "False" string from env; defaults to False in production
DEBUG = os.getenv('DEBUG', 'False') == 'True'

# Build ALLOWED_HOSTS: localhost + anything in env + Render's auto-injected hostname
_allowed_env = os.getenv('ALLOWED_HOSTS', '')
ALLOWED_HOSTS = ['localhost', '127.0.0.1']
if _allowed_env:
    ALLOWED_HOSTS += [h.strip() for h in _allowed_env.split(',') if h.strip()]

# Render injects RENDER_EXTERNAL_HOSTNAME automatically — add it if present
_render_host = os.getenv('RENDER_EXTERNAL_HOSTNAME')
if _render_host:
    ALLOWED_HOSTS.append(_render_host)

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
]

AUTHENTICATION_BACKENDS = [
    'django.contrib.auth.backends.ModelBackend',
    'allauth.account.auth_backends.AuthenticationBackend',
]

# ── MIDDLEWARE ────────────────────────────────────────────────────────────────
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
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

if DATABASE_URL:
    DATABASES = {
        'default': dj_database_url.config(
            default=DATABASE_URL,
            conn_max_age=600,
            conn_health_checks=True,
        )
    }
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
STATICFILES_DIRS = [_project_static] if _project_static.is_dir() else []

# WhiteNoise: compressed + cached static files for production
STATICFILES_STORAGE = 'whitenoise.storage.CompressedStaticFilesStorage'
# ── MEDIA ─────────────────────────────────────────────────────────────────────
MEDIA_URL = '/task_files/'
MEDIA_ROOT = BASE_DIR / 'task_files'

WHITENOISE_USE_FINDERS = True

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
CSRF_TRUSTED_ORIGINS = ['https://*.onrender.com']

_site_url = os.environ.get('SITE_URL', '').rstrip('/')
if _site_url and _site_url not in CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS.append(_site_url)

# ── AUTH ──────────────────────────────────────────────────────────────────────
SITE_ID = 1

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

SOCIALACCOUNT_PROVIDERS = {
    'google': {
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

# Allow overriding the SMTP provider via env vars (e.g. switch to Resend/Mailgun).
# Defaults to Gmail if not specified.
EMAIL_HOST    = _env('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT    = int(_env('EMAIL_PORT', '587'))
EMAIL_USE_TLS = _env('EMAIL_USE_TLS', 'True').lower() not in ('false', '0', 'no')
EMAIL_USE_SSL = _env('EMAIL_USE_SSL', 'False').lower() in ('true', '1', 'yes')

if EMAIL_HOST_USER == 'resend' and EMAIL_HOST_PASSWORD:
    # Use Resend HTTP API — avoids SMTP port restrictions on Render free tier
    EMAIL_BACKEND = 'core.email_backends.ResendAPIBackend'
elif EMAIL_HOST_USER and EMAIL_HOST_PASSWORD:
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
    EMAIL_TIMEOUT = 30
else:
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

DEFAULT_FROM_EMAIL = _env('DEFAULT_FROM_EMAIL', EMAIL_HOST_USER or 'noreply@taskforge.app')

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
