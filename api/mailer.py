"""Transactional email over HTTPS APIs (Render's free plan blocks outbound SMTP ports, so no SMTP in production).

Configured with environment variables only:
    BABYCARE_EMAIL_PROVIDER   brevo | resend | smtp (local/dev only) | dev-outbox (tests only, refused in production) | ''
    BABYCARE_EMAIL_API_KEY    provider API key (secret; Render env var, never in the repository)
    BABYCARE_EMAIL_FROM       verified sender, e.g. "HealthBook AI <you@example.com>"
    BABYCARE_SMTP_HOST / _PORT / _USER / _PASSWORD   only for the smtp provider on a local PC

Message bodies and recipient addresses are never logged. Failures are logged by class name only.
"""
import json
import logging
import os
import re
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage

log = logging.getLogger('healthbook.mailer')
# Tests replace this with a list to capture messages instead of sending.
OUTBOX = None


def provider():
    return os.getenv('BABYCARE_EMAIL_PROVIDER', '').strip().lower()


def configured():
    p = provider()
    if p in ('brevo', 'resend'):
        return bool(os.getenv('BABYCARE_EMAIL_API_KEY') and os.getenv('BABYCARE_EMAIL_FROM'))
    if p == 'smtp':
        return bool(os.getenv('BABYCARE_SMTP_HOST') and os.getenv('BABYCARE_EMAIL_FROM'))
    if p == 'dev-outbox':
        from .config import PRODUCTION
        return not PRODUCTION
    return OUTBOX is not None


def _sender():
    raw = os.getenv('BABYCARE_EMAIL_FROM', '')
    m = re.match(r'\s*(.*?)\s*<([^>]+)>\s*$', raw)
    return (m[1] or 'HealthBook AI', m[2]) if m else ('HealthBook AI', raw.strip())


def _post(url, headers, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method='POST',
                                 headers={'Content-Type': 'application/json', 'Accept': 'application/json', **headers})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status


def send(to, subject, text, html):
    """Send one message. Returns True when the provider accepted it. Never raises."""
    if OUTBOX is not None:
        OUTBOX.append({'to': to, 'subject': subject, 'text': text, 'html': html})
        return True
    p = provider()
    name, address = _sender()
    try:
        if p == 'brevo':
            status = _post('https://api.brevo.com/v3/smtp/email', {'api-key': os.environ['BABYCARE_EMAIL_API_KEY']},
                           {'sender': {'name': name, 'email': address}, 'to': [{'email': to}], 'subject': subject,
                            'textContent': text, 'htmlContent': html})
            return 200 <= status < 300
        if p == 'resend':
            status = _post('https://api.resend.com/emails', {'Authorization': 'Bearer ' + os.environ['BABYCARE_EMAIL_API_KEY']},
                           {'from': f'{name} <{address}>', 'to': [to], 'subject': subject, 'text': text, 'html': html})
            return 200 <= status < 300
        if p == 'smtp':
            msg = EmailMessage()
            msg['From'] = f'{name} <{address}>'; msg['To'] = to; msg['Subject'] = subject
            msg.set_content(text); msg.add_alternative(html, subtype='html')
            with smtplib.SMTP(os.environ['BABYCARE_SMTP_HOST'], int(os.getenv('BABYCARE_SMTP_PORT', '587')), timeout=15) as s:
                s.starttls()
                if os.getenv('BABYCARE_SMTP_USER'):
                    s.login(os.environ['BABYCARE_SMTP_USER'], os.environ['BABYCARE_SMTP_PASSWORD'])
                s.send_message(msg)
            return True
        if p == 'dev-outbox':
            from .config import PRODUCTION, DATA_DIR
            if PRODUCTION:
                log.warning('email not sent: dev-outbox is disabled in production');return False
            with open(DATA_DIR / 'dev-outbox.jsonl', 'a', encoding='utf-8') as fh:  # local test data directory only
                fh.write(json.dumps({'to': to, 'subject': subject, 'text': text}) + chr(10))
            return True
        log.warning('email not sent: no email provider configured')
        return False
    except urllib.error.HTTPError as exc:
        log.warning('email provider rejected the message: HTTP %s', exc.code)
    except Exception as exc:  # network, auth, config
        log.warning('email send failed: %s', type(exc).__name__)
    return False
