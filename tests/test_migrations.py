"""A real populated legacy database survives migration without losing originals."""
import os
import subprocess
import sys
from pathlib import Path

def test_legacy_migration_encrypts_and_preserves(tmp_path):
    root=Path(__file__).resolve().parents[1]
    env={**os.environ,'BABYCARE_DATA_DIR':str(tmp_path),'PYTHONPATH':str(root)}
    env.pop('DATABASE_URL',None)
    code=r'''
from datetime import date
from pathlib import Path
import json
from sqlalchemy import create_engine,text,inspect
from migrations.legacy_schema import Base
from api.config import DATA_DIR,DATABASE_URL
from api.crypto import encrypt,decrypt
engine=create_engine(DATABASE_URL)
Base.metadata.create_all(engine)
with engine.begin() as c:
    c.execute(text("INSERT INTO users(id,email,password_salt,password_hash,created_at) VALUES(1,'synthetic@test.invalid','salt','hash','2026-01-01')"))
    c.execute(text("INSERT INTO patients(id,user_id,name,dob,tz,created_at) VALUES(1,1,'Fictional legacy child','2025-01-01','Asia/Kolkata','2026-01-01')"))
    c.execute(text("INSERT INTO medicines(id,patient_id,name,instructions,start_date,schedule_times,status,version,created_at) VALUES(1,1,'Fictional calcium','Keep original wording','2026-01-01','','draft',1,'2026-01-01')"))
    c.execute(text("INSERT INTO medication_events(id,medicine_id,occurred_at,event_type,source,note,idempotency_key,created_at) VALUES(1,1,'2026-01-03T09:00','given','caregiver_report','Synthetic event','legacy-event','2026-01-03')"))
from api.migrate import upgrade
upgrade();upgrade()
from api.database import SessionLocal
from api.models import Medicine,Patient,MedicationEvent
with SessionLocal() as db:
    m=db.get(Medicine,1)
    assert m.name=='Fictional calcium' and m.instructions=='Keep original wording'
    assert m.end_date is None and m.actual_stop_date is None and m.version==1
    assert db.get(Patient,1).name=='Fictional legacy child'
    assert db.get(MedicationEvent,1).note=='Synthetic event'
with engine.connect() as c:
    raw=c.execute(text('SELECT name,instructions FROM medicines')).first()
    assert raw[0].startswith('enc:') and 'Fictional' not in raw[0]
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from api.config import ROOT
    head=ScriptDirectory.from_config(Config(str(ROOT/'alembic.ini'))).get_current_head()
    assert c.execute(text('SELECT version_num FROM alembic_version')).scalar()==head
    assert 'record_revisions' in inspect(c).get_table_names()
    assert 'source_ref' in {col['name'] for col in inspect(c).get_columns('documents')}
    assert 'course_type' in {col['name'] for col in inspect(c).get_columns('medicines')}
    assert not c.execute(text('PRAGMA foreign_key_check')).all()
    assert any(f['constrained_columns']==['admission_id'] for f in inspect(c).get_foreign_keys('hospital_visits'))
backups=list(DATA_DIR.glob('before-migration-*.sqlite.enc'))
assert len(backups)==1
assert decrypt(backups[0].read_bytes()).startswith(b'SQLite format 3')
'''
    result=subprocess.run([sys.executable,'-c',code],cwd=root,env=env,text=True,capture_output=True)
    assert result.returncode==0,result.stderr
