"""
Dumps all app data to a JSON file at BASE_DIR/data_export.json
Run: python manage.py export_data
Then download the file from Render via the /download-export/ URL (added temporarily).
"""
import json
from django.core.management.base import BaseCommand
from django.core.management import call_command
from django.conf import settings
import io


class Command(BaseCommand):
    help = "Export all data to BASE_DIR/data_export.json"

    EXPORT_APPS = [
        'auth.user',
        'auth.group',
        'sites.site',
        'core.profile',
        'core.project',
        'core.projectmembership',
        'core.task',
        'core.label',
        'core.comment',
        'core.taskattachment',
        'core.taskactivity',
        'core.team',
        'core.projectinvite',
        'core.notification',
        'core.rolepermission',
        'core.departmentpipelinesettings',
        'core.itdepartment',
        'core.itteam',
        'core.projectstageconfiguration',
        'socialaccount.socialapp',
        'socialaccount.socialaccount',
        'socialaccount.socialtoken',
    ]

    def handle(self, *args, **options):
        out = io.StringIO()
        try:
            call_command('dumpdata', *self.EXPORT_APPS,
                         indent=2, stdout=out, natural_foreign=True, natural_primary=True)
            export_path = settings.BASE_DIR / 'data_export.json'
            export_path.write_text(out.getvalue(), encoding='utf-8')
            self.stdout.write(self.style.SUCCESS(f"Export saved to {export_path}"))
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Export failed: {e}"))
