"""
Custom email backend that sends via Resend HTTP API instead of SMTP.
Avoids outbound port 465/587 restrictions on platforms like Render free tier.
Uses EMAIL_HOST_PASSWORD as the Resend API key (EMAIL_HOST_USER must be 'resend').
"""
import requests as _requests
from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend


class ResendAPIBackend(BaseEmailBackend):

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
                'from': message.from_email,
                'to':   list(message.to),
                'subject': message.subject,
                'text': message.body,
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
