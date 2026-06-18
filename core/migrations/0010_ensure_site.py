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

    Site.objects.update_or_create(
        pk=settings.SITE_ID,
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
