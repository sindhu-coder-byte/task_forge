"""
Custom HTTP-based email backends for platforms (e.g. Render free tier)
that block outbound SMTP ports 25, 465, and 587.

Both backends use HTTPS (port 443) so they are never blocked.

ResendAPIBackend  — activated when EMAIL_HOST_USER='resend'
                    Limitation: onboarding@resend.dev can only deliver
                    to the Resend account owner's email without a
                    verified custom domain.

BrevoAPIBackend   — activated when BREVO_API_KEY env var is set
                    No domain verification needed — verify a single
                    sender email in the Brevo dashboard and you can
                    deliver to any recipient.  Free: 300 emails/day.
"""
import re as _re
import requests as _requests
from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend


def _parse_from(from_email):
    """Split 'Name <addr>' into (name, addr). Falls back to ('TaskForge', raw)."""
    m = _re.match(r'^(.+?)\s*<(.+?)>\s*$', from_email or '')
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return 'TaskForge', (from_email or '').strip()


class BrevoAPIBackend(BaseEmailBackend):
    """
    Send via Brevo (ex-Sendinblue) transactional API.
    Set BREVO_API_KEY in your environment.
    Verify your sender address once in the Brevo dashboard — then you
    can send to ANY recipient without owning a custom domain.
    API docs: https://developers.brevo.com/reference/sendtransacemail
    """

    BREVO_URL = 'https://api.brevo.com/v3/smtp/email'

    def send_messages(self, email_messages):
        api_key = getattr(settings, 'BREVO_API_KEY', None)
        if not api_key:
            print('[TF-EMAIL] BrevoAPIBackend: BREVO_API_KEY not set')
            return 0

        default_from = getattr(settings, 'DEFAULT_FROM_EMAIL', '')
        sender_name, sender_email = _parse_from(default_from)

        sent = 0
        for message in email_messages:
            # Use the message's own from_email if different from the default
            msg_name, msg_email = _parse_from(message.from_email)

            payload = {
                'sender':      {'name': msg_name,  'email': msg_email},
                'to':          [{'email': addr} for addr in message.to],
                'subject':     message.subject,
                'textContent': message.body,
            }
            for content, mimetype in getattr(message, 'alternatives', []):
                if mimetype == 'text/html':
                    payload['htmlContent'] = content
                    break

            print(f"[TF-EMAIL] Brevo → from={msg_email} to={message.to} subject={message.subject!r}")

            try:
                resp = _requests.post(
                    self.BREVO_URL,
                    headers={
                        'api-key':      api_key,
                        'Content-Type': 'application/json',
                    },
                    json=payload,
                    timeout=30,
                )
                if resp.status_code in (200, 201, 202):
                    sent += 1
                    print(f"[TF-EMAIL] Brevo OK ({resp.status_code}) → {message.to}")
                else:
                    print(f"[TF-EMAIL] Brevo error {resp.status_code}: {resp.text[:300]}")
                    if not self.fail_silently:
                        raise Exception(f"Brevo API {resp.status_code}: {resp.text[:300]}")
            except _requests.RequestException as e:
                print(f"[TF-EMAIL] Brevo network error: {e}")
                if not self.fail_silently:
                    raise
            except Exception as e:
                print(f"[TF-EMAIL] Brevo exception: {e}")
                if not self.fail_silently:
                    raise

        return sent


class ResendAPIBackend(BaseEmailBackend):
    """
    Send via Resend HTTP API.
    Uses EMAIL_HOST_PASSWORD as the API key (EMAIL_HOST_USER must be 'resend').
    NOTE: onboarding@resend.dev can only deliver to your own Resend account
    email. Verify a custom domain in Resend to send to any address.
    """

    RESEND_URL = 'https://api.resend.com/emails'

    def send_messages(self, email_messages):
        api_key = (
            getattr(settings, 'RESEND_API_KEY', None)
            or getattr(settings, 'EMAIL_HOST_PASSWORD', None)
        )
        if not api_key:
            print('[TF-EMAIL] ResendAPIBackend: no API key — set EMAIL_HOST_PASSWORD')
            return 0

        sent = 0
        for message in email_messages:
            payload = {
                'from':    message.from_email,
                'to':      list(message.to),
                'subject': message.subject,
                'text':    message.body,
            }
            for content, mimetype in getattr(message, 'alternatives', []):
                if mimetype == 'text/html':
                    payload['html'] = content
                    break

            print(f"[TF-EMAIL] Resend → from={payload['from']} to={payload['to']} subject={payload['subject']!r}")

            try:
                resp = _requests.post(
                    self.RESEND_URL,
                    headers={
                        'Authorization': f'Bearer {api_key}',
                        'Content-Type':  'application/json',
                    },
                    json=payload,
                    timeout=30,
                )
                if resp.status_code in (200, 201, 202):
                    sent += 1
                    print(f"[TF-EMAIL] Resend OK ({resp.status_code}) → {message.to}")
                else:
                    print(f"[TF-EMAIL] Resend error {resp.status_code}: {resp.text[:300]}")
                    if not self.fail_silently:
                        raise Exception(f"Resend API {resp.status_code}: {resp.text[:300]}")
            except _requests.RequestException as e:
                print(f"[TF-EMAIL] Resend network error: {e}")
                if not self.fail_silently:
                    raise
            except Exception as e:
                print(f"[TF-EMAIL] Resend exception: {e}")
                if not self.fail_silently:
                    raise

        return sent
