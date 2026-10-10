"""Run separately: python -m api.worker. No patient data is logged."""
import os
import time
import logging
from datetime import timedelta
import httpx
from sqlalchemy import text
from filelock import FileLock, Timeout
from .config import DATA_DIR
from .database import SessionLocal, engine
from .models import Patient, Notification, PushDevice, Reminder, CaregiverAccess, WorkerHeartbeat, utcnow
from .reminders import reconcile, aware, source_active
from . import push_web

log=logging.getLogger('babycare.worker')
PUSH_URL='https://exp.host/--/api/v2/push/'

def deliver(db,n,client,now):
    device=db.get(PushDevice,n.device_id)
    reminder=db.get(Reminder,n.reminder_id)
    patient=db.get(Patient,reminder.patient_id) if reminder else None
    allowed=patient and (patient.user_id==n.user_id or db.query(CaregiverAccess).filter_by(patient_id=patient.id,user_id=n.user_id).first())
    if not allowed or not device or not device.enabled or reminder.status in ('cancelled','acknowledged'):
        n.status='cancelled';return
    if n.status!='accepted' and not source_active(db,reminder):
        n.status='cancelled';return
    if n.status!='accepted' and aware(reminder.snoozed_until or reminder.due_at)<now-timedelta(days=1):
        n.status='expired';return
    if device.token.startswith(push_web.WEBPUSH_PREFIX):
        deliver_web_push(db,n,device,now);return
    if os.getenv('BABYCARE_PUSH_ENABLED','false').lower()!='true':
        n.error_code='push_not_configured';n.next_attempt_at=now+timedelta(minutes=5);return
    headers={}
    if os.getenv('EXPO_ACCESS_TOKEN'):headers['Authorization']='Bearer '+os.environ['EXPO_ACCESS_TOKEN']
    try:
        if n.status=='accepted':
            response=client.post(PUSH_URL+'getReceipts',json={'ids':[n.receipt_id]},headers=headers)
            response.raise_for_status()
            receipt=response.json().get('data',{}).get(n.receipt_id)
            if not receipt:
                if aware(n.created_at)<now-timedelta(hours=23):n.status='receipt_unknown';n.error_code='receipt_expired'
                else:n.next_attempt_at=now+timedelta(minutes=15)
                return
            if receipt.get('status')=='ok':n.status='provider_received';return
            code=receipt.get('details',{}).get('error','provider_error')
            n.error_code=code;n.status='failed'
            if code=='DeviceNotRegistered':device.enabled=False
            return
        n.attempts+=1
        # No name, diagnosis, medicine or appointment details leave the server.
        response=client.post(PUSH_URL+'send',headers=headers,json={'to':device.token,'title':'HealthBook AI','body':'You have a confirmed care reminder. Open HealthBook AI to review it.','sound':'default','data':{'notification_id':n.id},'ttl':3600})
        response.raise_for_status()
        ticket=response.json().get('data',{})
        if isinstance(ticket,list):ticket=ticket[0]
        if ticket.get('status')=='ok' and ticket.get('id'):
            n.status='accepted';n.receipt_id=ticket['id'];n.next_attempt_at=now+timedelta(minutes=15);n.error_code=None
        else:
            code=ticket.get('details',{}).get('error','provider_error')
            n.error_code=code;n.status='failed'
            if code=='DeviceNotRegistered':device.enabled=False
            if code=='MessageRateExceeded':n.status='retry';n.next_attempt_at=now+timedelta(minutes=5)
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        n.error_code='transport_or_provider_error'
        # A receipt lookup outage must never resend an already accepted push.
        if n.status=='accepted':
            n.status='receipt_unknown' if aware(n.created_at)<now-timedelta(hours=23) else 'accepted'
            n.next_attempt_at=now+timedelta(minutes=15)
            return
        n.status='retry' if n.attempts<6 else 'failed'
        n.next_attempt_at=now+timedelta(seconds=min(3600,30*2**n.attempts))

def deliver_web_push(db,n,device,now):
    """Browser (VAPID) push: same generic sentence, same retry discipline as the Expo channel."""
    if not push_web.configured():
        n.error_code='webpush_not_configured';n.next_attempt_at=now+timedelta(minutes=5);return
    n.attempts+=1
    try:subscription=push_web.parse_subscription(device.token)
    except ValueError:
        n.status='failed';n.error_code='invalid_subscription';device.enabled=False;return
    outcome,code=push_web.send(subscription,n.id)
    if outcome=='delivered':n.status='provider_received';n.error_code=None
    elif outcome=='gone':n.status='failed';n.error_code=code;device.enabled=False
    elif outcome=='retry':
        n.error_code=code;n.status='retry' if n.attempts<6 else 'failed';n.next_attempt_at=now+timedelta(seconds=min(3600,30*2**n.attempts))
    else:n.status='failed';n.error_code=code

def tick(now=None,client=None):
    now=now or utcnow()
    with SessionLocal() as db:
        for p in db.query(Patient).all():reconcile(db,p,now)
        with httpx.Client(timeout=15) if client is None else _Borrowed(client) as transport:
            rows=db.query(Notification).filter(Notification.channel=='expo',Notification.status.in_(['pending','retry','accepted']),Notification.next_attempt_at<=now).limit(100).all()
            for n in rows:
                deliver(db,n,transport,now);db.commit()
        heartbeat=db.get(WorkerHeartbeat,1)
        if not heartbeat:db.add(WorkerHeartbeat(id=1,at=now))
        else:heartbeat.at=now
        db.commit()

class _Borrowed:
    def __init__(self,client):self.client=client
    def __enter__(self):return self.client
    def __exit__(self,*args):pass

def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    from .migrate import upgrade
    upgrade()
    lock=FileLock(str(DATA_DIR/'reminder-worker.lock'),timeout=0)
    with lock,engine.connect() as connection:
        if engine.dialect.name=='postgresql':
            if not connection.execute(text('SELECT pg_try_advisory_lock(82476124)')).scalar():raise RuntimeError('Worker already running')
        log.info('Reminder worker started')
        while True:
            try:tick()
            except Exception:log.error('worker_tick_failed')
            if os.getenv('BABYCARE_WORKER_ONCE')=='1':break
            time.sleep(15)

if __name__=='__main__':main()
