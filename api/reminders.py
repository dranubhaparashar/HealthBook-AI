"""Confirmed plans -> persisted notifications, never administration records."""
from datetime import datetime, timedelta, time, timezone
from zoneinfo import ZoneInfo
from sqlalchemy.exc import IntegrityError
from .models import Medicine, Vaccine, HospitalVisit, Reminder, Notification, CaregiverAccess, PushDevice, utcnow

def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

def local_time(value, tz):
    """Reject nonexistent or ambiguous wall times rather than choosing a DST fold."""
    value=value.replace(tzinfo=None)
    zone=ZoneInfo(tz)
    first=value.replace(tzinfo=zone,fold=0)
    second=value.replace(tzinfo=zone,fold=1)
    if first.utcoffset()!=second.utcoffset() or first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)!=value:
        raise ValueError('Ambiguous or nonexistent local clock time; confirm a different appointment time')
    return first


def source_active(db, reminder):
    """Versioned source validity is independent of the notification viewing window."""
    if reminder.kind=='medicine':
        row=db.get(Medicine,reminder.item_id)
        return bool(row and row.status=='verified' and reminder.key.startswith(f'med-{row.id}-v{row.version}-'))
    if reminder.kind=='vaccine':
        row=db.get(Vaccine,reminder.item_id)
        return bool(row and row.confirmed and not row.given_date and row.due_date and row.source_document_id and row.source_type!='India_UIP_reference' and reminder.key.startswith(f'vaccine-{row.id}-v{row.version}-'))
    row=db.get(HospitalVisit,reminder.item_id)
    return bool(row and row.confirmed and row.status=='planned' and reminder.key.startswith(f'visit-{row.id}-v{row.version}-'))

def reminder_items(db, p, days, now=None, lookback=0):
    now=now or utcnow()
    today=now.astimezone(ZoneInfo(p.tz)).date()-timedelta(days=lookback)
    until=now.astimezone(ZoneInfo(p.tz)).date()+timedelta(days=days)
    entries=[]
    def add(key,kind,title,when,row):
        if not today<=when.date()<=until:return
        try: instant=local_time(when,p.tz)
        except ValueError:return  # No guessed DST instructions; surfaced in schedule warnings.
        entries.append(dict(key=key,kind=kind,title=title,at=instant.isoformat(),source_document_id=row.source_document_id,item_id=row.id,
            safety='Confirmed appointment or prescription reminder only; delivery does not record administration.'))
    for m in db.query(Medicine).filter_by(patient_id=p.id,status='verified'):
        if not (m.start_date and m.end_date and m.schedule_times):continue
        cursor=max(today,m.start_date)
        end=min(until,m.end_date,m.actual_stop_date or until)
        while cursor<=end:
            for clock in m.schedule_times.split(','):
                add(f'med-{m.id}-v{m.version}-{cursor}-{clock}','medicine',f'Medication reminder: {m.name}',datetime.combine(cursor,time.fromisoformat(clock)),m)
            cursor+=timedelta(days=1)
    for v in db.query(HospitalVisit).filter_by(patient_id=p.id,confirmed=True,status='planned'):
        add(f'visit-{v.id}-v{v.version}-{v.planned_at.isoformat()}',v.kind,f'Appointment: {v.purpose}',v.planned_at,v)
        add(f'visit-{v.id}-v{v.version}-day-before',v.kind,f'Appointment tomorrow: {v.purpose}',datetime.combine(v.planned_at.date()-timedelta(days=1),time(9)),v)
    for v in db.query(Vaccine).filter_by(patient_id=p.id,confirmed=True).filter(Vaccine.given_date.is_(None)):
        if not v.due_date or not v.source_document_id or v.source_type=='India_UIP_reference':continue
        for lead in sorted(set(map(int,v.reminder_days.split(','))),reverse=True):
            when=datetime.combine(v.due_date-timedelta(days=lead),time(9))
            add(f'vaccine-{v.id}-v{v.version}-{v.due_date}-{lead}d','vaccine',f'Vaccination appointment {v.due_date}: {v.name} {v.dose_label}',when,v)
    return sorted(entries,key=lambda x:x['at'])

def reconcile(db,p,now=None):
    now=now or utcnow()
    planned=reminder_items(db,p,35,now,lookback=1)
    keys={x['key'] for x in planned}
    existing={r.key:r for r in db.query(Reminder).filter_by(patient_id=p.id)}
    for row in existing.values():
        if row.key not in keys and row.status in ('pending','notified','snoozed') and not source_active(db,row):
            # Stop changed/revised/cancelled orders, including outstanding delivery retries.
            row.status='cancelled'
            db.query(Notification).filter(Notification.reminder_id==row.id,Notification.status.in_(['pending','retry'])).update({'status':'cancelled'})
        elif row.status=='pending' and aware(row.due_at)<now-timedelta(days=1):
            # Do not deliver stale medication instructions after a prolonged outage.
            row.status='expired'
            db.query(Notification).filter(Notification.reminder_id==row.id,Notification.status.in_(['pending','retry'])).update({'status':'expired'})
    for item in planned:
        if item['key'] in existing:continue
        db.add(Reminder(patient_id=p.id,key=item['key'],kind=item['kind'],item_id=item['item_id'],title=item['title'],due_at=datetime.fromisoformat(item['at']).astimezone(timezone.utc)))
    db.flush()
    recipients={p.user_id,*[g.user_id for g in db.query(CaregiverAccess).filter_by(patient_id=p.id)]}
    for row in db.query(Reminder).filter(Reminder.patient_id==p.id,Reminder.status.in_(['pending','snoozed'])):
        when=row.snoozed_until or row.due_at
        if aware(when)>now:continue
        cycle=row.snoozed_until.isoformat() if row.snoozed_until else 'initial'
        for uid in recipients:
            channels=[('in_app',None)]+[('expo',d.id) for d in db.query(PushDevice).filter_by(user_id=uid,enabled=True)]
            for channel,did in channels:
                key=f'{row.id}:{uid}:{did}:{cycle}'
                if db.query(Notification).filter_by(dedupe_key=key).first():continue
                db.add(Notification(reminder_id=row.id,user_id=uid,device_id=did,channel=channel,dedupe_key=key,status='available' if channel=='in_app' else 'pending',next_attempt_at=now))
        row.status='notified'
    db.commit()
