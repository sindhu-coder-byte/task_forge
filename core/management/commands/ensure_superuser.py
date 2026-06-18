"""
One-shot site setup command for Render builds.

Creates/repairs superuser, admin Profile, and Google SocialApp from env vars.

Required env vars:
  DJANGO_SUPERUSER_USERNAME
  DJANGO_SUPERUSER_PASSWORD
  DJANGO_SUPERUSER_EMAIL     (optional)
  GOOGLE_OAUTH2_CLIENT_ID    (optional — skips Google SocialApp if absent)
  GOOGLE_OAUTH2_CLIENT_SECRET
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
        self._ensure_google_socialapp()

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

            site, created = Site.objects.update_or_create(
                pk=settings.SITE_ID,
                defaults={"domain": domain, "name": domain},
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

    # ------------------------------------------------------------------ #
    def _ensure_google_socialapp(self):
        client_id     = os.environ.get("GOOGLE_OAUTH2_CLIENT_ID", "").strip()
        client_secret = os.environ.get("GOOGLE_OAUTH2_CLIENT_SECRET", "").strip()

        if not client_id or not client_secret:
            self.stdout.write(self.style.WARNING(
                "GOOGLE_OAUTH2_CLIENT_ID / _SECRET not set — skipping Google SocialApp."
            ))
            return

        try:
            from django.contrib.sites.models import Site
            from allauth.socialaccount.models import SocialApp

            site = Site.objects.get_or_create(pk=settings.SITE_ID)[0]

            app, created = SocialApp.objects.get_or_create(
                provider='google',
                defaults={
                    'name':          'Google',
                    'client_id':     client_id,
                    'secret':        client_secret,
                },
            )

            if not created:
                # Keep credentials in sync with env vars
                updated = False
                if app.client_id != client_id:
                    app.client_id = client_id
                    updated = True
                if app.secret != client_secret:
                    app.secret = client_secret
                    updated = True
                if updated:
                    app.save(update_fields=['client_id', 'secret'])

            # Link to the active django.contrib.sites row.
            if not app.sites.filter(pk=settings.SITE_ID).exists():
                app.sites.add(site)

            verb = "created" if created else "synced"
            self.stdout.write(self.style.SUCCESS(
                f"Google SocialApp {verb} and linked to site '{site.domain}'."
            ))
        except Exception as exc:
            self.stderr.write(self.style.ERROR(f"Google SocialApp setup failed: {exc}"))
