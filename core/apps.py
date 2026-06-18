from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'

    def ready(self):
        import core.signals  # noqa: F401
        from django.db.models.signals import post_migrate
        post_migrate.connect(_sync_site_domain, sender=self)
        post_migrate.connect(_ensure_superuser, sender=self)


def _sync_site_domain(**_kwargs):
    """Keep django.contrib.sites in sync with SITE_URL / RENDER_EXTERNAL_HOSTNAME."""
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
        try:
            site_id = settings.SITE_ID
            existing_for_domain = Site.objects.filter(domain=domain).first()
            if existing_for_domain and existing_for_domain.pk != site_id:
                domain_for_site_id = f'site-{site_id}.{domain}'
            else:
                domain_for_site_id = domain

            Site.objects.update_or_create(
                pk=site_id,
                defaults={'domain': domain_for_site_id, 'name': domain},
            )
        except Exception as exc:
            print(f'[core.apps._sync_site_domain] ERROR syncing site domain: {exc}')


def _ensure_superuser(**_kwargs):
    """Create/repair superuser from env vars after every migrate run."""
    import os
    username = os.environ.get('DJANGO_SUPERUSER_USERNAME', '').strip()
    password = os.environ.get('DJANGO_SUPERUSER_PASSWORD', '').strip()
    email    = os.environ.get('DJANGO_SUPERUSER_EMAIL', '').strip()

    if not username or not password:
        return

    try:
        from django.contrib.auth.models import User
        from core.models import Profile

        user, created = User.objects.get_or_create(username=username)
        user.email = email
        user.is_staff = True
        user.is_superuser = True
        user.set_password(password)
        user.save()

        profile, _ = Profile.objects.get_or_create(user=user)
        if profile.role != 'admin':
            profile.role = 'admin'
            profile.save(update_fields=['role'])

        verb = 'created' if created else 'updated'
        print(f'[ensure_superuser] Superuser "{username}" {verb} successfully.')
    except Exception as exc:
        print(f'[ensure_superuser] ERROR: {exc}')
