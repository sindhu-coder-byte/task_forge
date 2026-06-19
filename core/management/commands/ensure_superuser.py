"""
One-shot site setup command for Render builds.

Creates/repairs superuser and admin Profile from env vars.
Google OAuth credentials come from SOCIALACCOUNT_PROVIDERS settings — no DB SocialApp needed.

Required env vars:
  DJANGO_SUPERUSER_USERNAME
  DJANGO_SUPERUSER_PASSWORD
  DJANGO_SUPERUSER_EMAIL     (optional)
"""
import os
from urllib.parse import urlparse

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create or repair superuser + Google OAuth app from env vars."

    def handle(self, *args, **options):
        self._ensure_site()
        self._ensure_superuser()

    # ------------------------------------------------------------------ #
    def _ensure_site(self):
        site_url = getattr(settings, "SITE_URL", "").rstrip("/")
        render_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "").strip()

        if site_url:
            domain = urlparse(site_url).netloc
        else:
            domain = render_host

        if not domain:
            self.stdout.write(self.style.WARNING(
                "SITE_URL / RENDER_EXTERNAL_HOSTNAME not set - skipping Site setup."
            ))
            return None

        try:
            from django.contrib.sites.models import Site

            site_id = settings.SITE_ID
            existing_for_domain = Site.objects.filter(domain=domain).first()
            if existing_for_domain and existing_for_domain.pk != site_id:
                domain_for_site_id = f"site-{site_id}.{domain}"
            else:
                domain_for_site_id = domain

            site, created = Site.objects.update_or_create(
                pk=site_id,
                defaults={"domain": domain_for_site_id, "name": domain},
            )

            verb = "created" if created else "synced"
            self.stdout.write(self.style.SUCCESS(
                f"Site {verb}: id={site.pk}, domain='{site.domain}'."
            ))
            return site
        except Exception as exc:
            self.stderr.write(self.style.ERROR(f"Site setup failed: {exc}"))
            return None

    # ------------------------------------------------------------------ #
    def _ensure_superuser(self):
        username = os.environ.get("DJANGO_SUPERUSER_USERNAME", "").strip()
        password = os.environ.get("DJANGO_SUPERUSER_PASSWORD", "").strip()
        email    = os.environ.get("DJANGO_SUPERUSER_EMAIL", "").strip()

        if not username or not password:
            self.stdout.write(self.style.WARNING(
                "DJANGO_SUPERUSER_USERNAME / _PASSWORD not set — skipping superuser."
            ))
            return

        try:
            user, created = User.objects.get_or_create(username=username)
            user.email = email
            user.is_staff = True
            user.is_superuser = True
            user.set_password(password)
            user.save()

            from core.models import Profile
            profile, _ = Profile.objects.get_or_create(user=user)
            if profile.role != 'admin':
                profile.role = 'admin'
                profile.save(update_fields=['role'])

            verb = "created" if created else "updated"
            self.stdout.write(self.style.SUCCESS(
                f"Superuser '{username}' {verb} (is_superuser=True, role=admin)."
            ))
        except Exception as exc:
            self.stderr.write(self.style.ERROR(f"Superuser setup failed: {exc}"))


