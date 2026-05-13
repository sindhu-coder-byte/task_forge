"""
Creates a superuser from environment variables if one doesn't already exist.

Required env vars:
  DJANGO_SUPERUSER_USERNAME
  DJANGO_SUPERUSER_PASSWORD
  DJANGO_SUPERUSER_EMAIL   (optional, defaults to empty)

Run automatically during Render build:
  python manage.py ensure_superuser
"""
import os

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create superuser from env vars if it does not already exist."

    def handle(self, *args, **options):
        username = os.environ.get("DJANGO_SUPERUSER_USERNAME", "").strip()
        password = os.environ.get("DJANGO_SUPERUSER_PASSWORD", "").strip()
        email    = os.environ.get("DJANGO_SUPERUSER_EMAIL", "").strip()

        if not username or not password:
            self.stdout.write("DJANGO_SUPERUSER_USERNAME / _PASSWORD not set — skipping.")
            return

        if User.objects.filter(username=username).exists():
            self.stdout.write(f"Superuser '{username}' already exists — skipping.")
            return

        User.objects.create_superuser(username=username, email=email, password=password)
        self.stdout.write(self.style.SUCCESS(f"Superuser '{username}' created."))
