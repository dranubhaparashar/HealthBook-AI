"""Standards-based Web Push (RFC 8030 + VAPID) for the installed web app.

Chrome, Edge and Android browsers deliver these through Firebase Cloud Messaging endpoints; Firefox and
Safari use their own push services. No Firebase SDK, Firebase project or paid plan is involved, and the
notification body is a fixed generic sentence: no name, medicine, diagnosis or appointment detail leaves the server.

Key generation (run once, store both values as Render environment variables):
    python -m api.push_web generate
"""
import base64
import json
import os
import sys

WEBPUSH_PREFIX = 'webpush:'
GENERIC_BODY = 'You have a confirmed care reminder. Open HealthBook AI to review it.'


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def generate_keys():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    private = ec.generate_private_key(ec.SECP256R1())
    private_raw = private.private_numbers().private_value.to_bytes(32, 'big')
    public_raw = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {'public_key': _b64url(public_raw), 'private_key': _b64url(private_raw)}


def public_key():
    return os.getenv('BABYCARE_VAPID_PUBLIC_KEY', '').strip()


def configured() -> bool:
    return bool(public_key() and os.getenv('BABYCARE_VAPID_PRIVATE_KEY', '').strip())


def parse_subscription(token: str) -> dict:
    if not token.startswith(WEBPUSH_PREFIX):
        raise ValueError('not a web push subscription')
    return json.loads(token[len(WEBPUSH_PREFIX):])


def send(subscription: dict, notification_id: int):
    """Returns (outcome, error_code). outcome is one of delivered | gone | retry | failed."""
    if not configured():
        return 'retry', 'webpush_not_configured'
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        return 'retry', 'webpush_library_missing'
    contact = os.getenv('BABYCARE_VAPID_CONTACT', 'mailto:admin@example.invalid')
    if not contact.startswith(('mailto:', 'https://')):
        contact = 'mailto:' + contact
    payload = json.dumps({'title': 'HealthBook AI', 'body': GENERIC_BODY, 'notification_id': notification_id})
    try:
        webpush(subscription_info=subscription, data=payload, vapid_private_key=os.environ['BABYCARE_VAPID_PRIVATE_KEY'].strip(),
                vapid_claims={'sub': contact}, ttl=3600, timeout=15)
        return 'delivered', None
    except WebPushException as exc:
        status = getattr(exc.response, 'status_code', None)
        if status in (404, 410):
            return 'gone', 'subscription_gone'
        if status in (429, 500, 502, 503, 504) or status is None:
            return 'retry', f'push_service_{status or "unreachable"}'
        return 'failed', f'push_service_{status}'
    except Exception:
        return 'retry', 'transport_error'


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'generate':
        keys = generate_keys()
        print('BABYCARE_VAPID_PUBLIC_KEY=' + keys['public_key'])
        print('BABYCARE_VAPID_PRIVATE_KEY=' + keys['private_key'])
        print('# Keep the private key secret (Render environment variable only). The public key also goes into web/config.js.')
    else:
        print(__doc__)
