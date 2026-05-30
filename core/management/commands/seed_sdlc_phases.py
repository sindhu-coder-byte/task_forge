from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'No longer needed. SDLC phases are now a built-in dropdown on Department.'

    def handle(self, *args, **options):
        self.stdout.write(
            self.style.WARNING(
                'seed_sdlc_phases is no longer required.\n'
                'SDLC phases are now a plain dropdown field on the Department model.\n'
                'Go to /admin/core/department/add/ to create departments directly.'
            )
        )
