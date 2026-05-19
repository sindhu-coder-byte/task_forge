"""
Custom email backend that sends via Resend HTTP API instead of SMTP.
Avoids outbound port 465/587 restrictions on platforms like Render free tier.
Uses the existing EMAIL_HOST_PASSWORD as the Resend API key.
"""
import requests as _requests
from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend


class ResendAPIBackend(BaseEmailBackend):

    def send_messages(self, email_messages):
        api_key = (
            getattr(settings, 'RESEND_API_KEY', None)
            or getattr(settings, 'EMAIL_HOST_PASSWORD', None)
        )
        if not api_key:
            return 0

        sent = 0
        for message in email_messages:
            payload = {
                'from': message.from_email,
                'to': list(message.to),
                'subject': message.subject,
                'text': message.body,
            }
            for content, mimetype in getattr(message, 'alternatives', []):
                if mimetype == 'text/html':
                    payload['html'] = content
                    break

            try:
                resp = _requests.post(
                    'https://api.resend.com/emails',
                    headers={
                        'Authorization': f'Bearer {api_key}',
                        'Content-Type': 'application/json',
                    },
                    json=payload,
                    timeout=30,
                )
                if resp.status_code in (200, 201, 202):
                    sent += 1
                    print(f"[TF-EMAIL] Resend API OK → {message.to}")
                else:
                    print(f"[TF-EMAIL] Resend API error {resp.status_code}: {resp.text}")
                    if not self.fail_silently:
                        raise Exception(f"Resend API {resp.status_code}: {resp.text}")
            except Exception as e:
                print(f"[TF-EMAIL] Resend API exception: {e}")
                if not self.fail_silently:
                    raise

        return sent
