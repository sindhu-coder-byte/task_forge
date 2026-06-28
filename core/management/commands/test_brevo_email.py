"""
Diagnostic command: tests the active email backend end-to-end.

Usage (Render Shell or local):
    python manage.py test_brevo_email
    python manage.py test_brevo_email --to other@example.com
"""
from django.core.management.base import BaseCommand
from django.core.mail import EmailMultiAlternatives
from django.conf import settings


class Command(BaseCommand):
    help = 'Send a test email to verify the active email backend (Brevo/SMTP/console).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--to',
            default=None,
            help='Recipient address (default: DEFAULT_FROM_EMAIL address)',
        )

    def handle(self, *args, **options):
        backend = getattr(settings, 'EMAIL_BACKEND', 'not set')
        api_key = getattr(settings, 'BREVO_API_KEY', None)
        from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@VetriFlow.app')
        site_url = getattr(settings, 'SITE_URL', '')

        self.stdout.write('─' * 60)
        self.stdout.write(f'EMAIL_BACKEND   : {backend}')
        self.stdout.write(f'DEFAULT_FROM    : {from_email}')
        self.stdout.write(f'BREVO_API_KEY   : {"SET (" + api_key[:12] + "…)" if api_key else "NOT SET"}')
        self.stdout.write(f'SITE_URL        : {site_url}')
        self.stdout.write('─' * 60)

        # Derive recipient
        to_addr = options.get('to')
        if not to_addr:
            # Extract plain address from "Name <addr>" format
            import re
            m = re.search(r'<(.+?)>', from_email)
            to_addr = m.group(1) if m else from_email
        self.stdout.write(f'Sending test email → {to_addr}')

        html = (
            '<h2>VetriFlow — test email</h2>'
            '<p>If you received this, your email backend is working correctly.</p>'
            f'<p>Backend: <code>{backend}</code></p>'
            f'<p>From: <code>{from_email}</code></p>'
        )
        text = (
            'VetriFlow — test email\n\n'
            'If you received this, your email backend is working correctly.\n'
            f'Backend: {backend}\n'
            f'From: {from_email}\n'
        )

        try:
            msg = EmailMultiAlternatives(
                subject='[VetriFlow] Email backend test',
                body=text,
                from_email=from_email,
                to=[to_addr],
            )
            msg.attach_alternative(html, 'text/html')
            msg.send(fail_silently=False)
            self.stdout.write(self.style.SUCCESS(f'✓ send() returned without error → {to_addr}'))
            self.stdout.write('Check the inbox (and spam/junk folder) of that address.')
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'✗ send() raised: {e}'))
            self.stdout.write('Fix the error above, then redeploy and re-run this command.')
