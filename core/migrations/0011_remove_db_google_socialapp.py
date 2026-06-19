from django.db import migrations


def remove_google_db_app(apps, schema_editor):
    """
    Delete any DB-backed SocialApp for Google now that credentials come
    from SOCIALACCOUNT_PROVIDERS['google']['APP'] in settings.
    Having both a DB record and a settings record causes allauth's get_app()
    to raise MultipleObjectsReturned.
    """
    try:
        SocialApp = apps.get_model('socialaccount', 'SocialApp')
        deleted, _ = SocialApp.objects.filter(provider='google').delete()
        if deleted:
            print(f"[migration] Removed {deleted} DB Google SocialApp record(s).")
    except Exception as e:
        print(f"[migration] remove_google_db_app skipped: {e}")


class Migration(migrations.Migration):

    dependencies = [
        ('socialaccount', '0006_alter_socialaccount_extra_data'),
        ('core', '0010_ensure_site'),
    ]

    operations = [
        migrations.RunPython(remove_google_db_app, migrations.RunPython.noop),
    ]
