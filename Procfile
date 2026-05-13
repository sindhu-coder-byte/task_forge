web: python manage.py ensure_superuser; gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers 2 --timeout 120
