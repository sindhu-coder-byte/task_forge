import os
from urllib.parse import urlparse

from django.conf import settings
from django.db import migrations


def ensure_site(apps, schema_editor):
    Site = apps.get_model('sites', 'Site')

    site_url = getattr(settings, 'SITE_URL', '').rstrip('/')
    render_host = os.environ.get('RENDER_EXTERNAL_HOSTNAME', '').strip()

    if site_url:
        domain = urlparse(site_url).netloc
    else:
        domain = render_host

    if not domain:
        return

    site_id = settings.SITE_ID
    existing_for_domain = Site.objects.filter(domain=domain).first()
    if existing_for_domain and existing_for_domain.pk != site_id:
        alternate_domain = f'site-{site_id}.{domain}'
        Site.objects.update_or_create(
            pk=site_id,
            defaults={'domain': alternate_domain, 'name': domain},
        )
        return

    Site.objects.update_or_create(
        pk=site_id,
        defaults={'domain': domain, 'name': domain},
    )


class Migration(migrations.Migration):

    dependencies = [
        ('sites', '0002_alter_domain_unique'),
        ('core', '0009_add_invited_by_to_project_invite'),
    ]

    operations = [
        migrations.RunPython(ensure_site, migrations.RunPython.noop),
    ]
