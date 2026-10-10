from alembic import command
from alembic.config import Config
from .config import ROOT

def upgrade():
    # Serialize upgrades across API/worker startup. SQLite backup is encrypted.
    from filelock import FileLock
    from .config import DATA_DIR
    from .database import engine
    from sqlalchemy import inspect, text
    from .crypto import encrypt
    import sqlite3
    from datetime import datetime, timezone
    from alembic.script import ScriptDirectory
    config=Config(str(ROOT / 'alembic.ini'))
    head=ScriptDirectory.from_config(config).get_current_head()
    with FileLock(str(DATA_DIR/'migration.lock'),timeout=60):
        if engine.dialect.name=='sqlite' and engine.url.database:
            with engine.connect() as connection:
                tables=inspect(connection).get_table_names()
                version=connection.execute(text('SELECT version_num FROM alembic_version')).scalar() if 'alembic_version' in tables else None
                if 'patients' in tables and version!=head and connection.execute(text('SELECT count(*) FROM patients')).scalar():
                    with sqlite3.connect(engine.url.database) as source,sqlite3.connect(':memory:') as snapshot:
                        source.backup(snapshot)
                        name='before-migration-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'.sqlite.enc'
                        (DATA_DIR/name).write_bytes(encrypt(snapshot.serialize()))
        command.upgrade(config, 'head')

if __name__ == '__main__':
    upgrade()
