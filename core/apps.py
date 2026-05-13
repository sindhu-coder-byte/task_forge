from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'

    def ready(self):
        import core.signals  # noqa: F401
        from django.db.models.signals import post_migrate
        post_migrate.connect(_sync_site_domain, sender=self)
        # Also sync on every server startup so it takes effect without a migration run.
        _sync_site_domain()


def _sync_site_domain(**_kwargs):
    """Keep django.contrib.sites in sync with SITE_URL / RENDER_EXTERNAL_HOSTNAME.
    Runs automatically after every `manage.py migrate` (including Render's build step).
    """
    try:
        import os
        from urllib.parse import urlparse
        from django.conf import settings
        from django.contrib.sites.models import Site

        site_url = getattr(settings, 'SITE_URL', '').rstrip('/')
        render_host = os.environ.get('RENDER_EXTERNAL_HOSTNAME', '')

        if site_url:
            domain = urlparse(site_url).netloc
        elif render_host:
            domain = render_host
        else:
            return

        if domain:
            Site.objects.update_or_create(
                pk=settings.SITE_ID,
                defaults={'domain': domain, 'name': domain},
            )
    except Exception:
        pass
