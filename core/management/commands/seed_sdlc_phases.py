from django.core.management.base import BaseCommand
from core.models import SDLCPhase


PHASES = [
    ('requirements', 'Requirements & Backlog',   1),
    ('design',       'Design & Prototyping',      2),
    ('development',  'Development Sprint',        3),
    ('testing',      'QA & Testing',              4),
    ('deployment',   'Deployment & Release',      5),
    ('monitoring',   'Monitoring & Maintenance',  6),
]


class Command(BaseCommand):
    help = 'Seed the six canonical SDLC phases into the database.'

    def handle(self, *args, **options):
        created = 0
        for key, label, order in PHASES:
            _, was_created = SDLCPhase.objects.update_or_create(
                key=key,
                defaults={'label': label, 'order': order},
            )
            if was_created:
                created += 1

        self.stdout.write(
            self.style.SUCCESS(
                f'Done. {created} phase(s) created, {len(PHASES) - created} already existed.'
            )
        )
