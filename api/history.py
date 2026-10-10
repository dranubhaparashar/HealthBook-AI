"""Record earlier clinical values within the same transaction as their amendment."""
import json
from .models import RecordRevision, Patient

def remember(db,user,row):
    payload={c.name:getattr(row,c.name) for c in row.__table__.columns}
    db.add(RecordRevision(patient_id=row.id if isinstance(row,Patient) else row.patient_id,user_id=user.id,
        entity=row.__tablename__,entity_id=row.id,snapshot_json=json.dumps(payload,default=str)))
