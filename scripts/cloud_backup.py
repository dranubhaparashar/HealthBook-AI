"""User-controlled, encrypted FULL backup and restore of BabyCare AI (any database, any document store).

Free cloud plans pause projects and keep no automatic backups, so the family keeps its own copies:

    python scripts/cloud_backup.py create  backups/babycare-2026-10-05.full.enc
    python scripts/cloud_backup.py verify  backups/babycare-2026-10-05.full.enc
    python scripts/cloud_backup.py restore backups/babycare-2026-10-05.full.enc

Environment selects the SOURCE (create) or TARGET (restore): DATABASE_URL, BABYCARE_STORAGE (local|supabase),
SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY / SUPABASE_STORAGE_BUCKET, BABYCARE_DATA_DIR.
BABYCARE_BACKUP_KEY (a Fernet key kept OUTSIDE the backup location) encrypts the archive. The application key
(BABYCARE_FERNET_KEY / file_crypt.key) is NOT included unless --include-app-key is passed; keep it in a separate place
(password manager). Without it a restored copy is unreadable, which is also what protects a stolen backup.

Restore only ever writes into an EMPTY database and never overwrites an existing stored object.
"""
import argparse
import hashlib
import io
import json
import os
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCHEMA = 'babycare-full-backup-1'


def _key():
    key = os.getenv('BABYCARE_BACKUP_KEY', '').strip()
    if not key:
        raise SystemExit('Set BABYCARE_BACKUP_KEY (python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())") and keep it away from the backup files')
    from cryptography.fernet import Fernet
    return Fernet(key.encode())


def _app_key_bytes():
    from api.config import DATA_DIR
    env = os.getenv('BABYCARE_FERNET_KEY')
    if env:
        return env.encode()
    path = DATA_DIR / 'file_crypt.key'
    return path.read_bytes() if path.exists() else None


def create(destination, include_app_key=False):
    from sqlalchemy import text
    from api.database import engine
    from api.datasync import all_tables, dump_rows, row_counts
    from api.storage import get_storage
    tables = all_tables()
    store = get_storage()
    buf = io.BytesIO()
    manifest = {'schema': SCHEMA, 'created_at': datetime.now(timezone.utc).isoformat(), 'database': engine.dialect.name, 'storage': store.describe(),
                'tables': {}, 'objects': {}, 'includes_app_key': bool(include_app_key)}
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as archive, engine.connect() as connection:
        manifest['alembic_version'] = connection.execute(text('SELECT version_num FROM alembic_version')).scalar()
        counts = row_counts(connection, tables)
        for table in tables:
            rows = dump_rows(connection, table)
            manifest['tables'][table.name] = len(rows)
            if len(rows) != counts[table.name]:
                raise SystemExit(f'Table {table.name} changed while dumping; retry when the app is idle')
            archive.writestr(f'tables/{table.name}.json', json.dumps(rows, ensure_ascii=False))
        for name in store.list():
            blob = store.read(name)
            manifest['objects'][name] = hashlib.sha256(blob).hexdigest()
            archive.writestr(f'objects/{name}', blob)
        if include_app_key:
            key = _app_key_bytes()
            if not key:
                raise SystemExit('No application key found to include')
            archive.writestr('app_key', key)
        archive.writestr('manifest.json', json.dumps(manifest, indent=2))
    target = Path(destination).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('xb') as stream:
        stream.write(_key().encrypt(buf.getvalue()))
    return target, manifest


def _open(source):
    raw = _key().decrypt(Path(source).read_bytes())
    archive = zipfile.ZipFile(io.BytesIO(raw))
    bad = archive.testzip()
    if bad:
        raise SystemExit(f'Corrupt archive member: {bad}')
    manifest = json.loads(archive.read('manifest.json'))
    if manifest.get('schema') != SCHEMA:
        raise SystemExit('Unknown backup schema')
    return archive, manifest


def verify(source):
    archive, manifest = _open(source)
    problems = []
    for table, count in manifest['tables'].items():
        rows = json.loads(archive.read(f'tables/{table}.json'))
        if len(rows) != count:
            problems.append(f'{table}: {len(rows)} rows, manifest says {count}')
    for name, digest in manifest['objects'].items():
        if hashlib.sha256(archive.read(f'objects/{name}')).hexdigest() != digest:
            problems.append(f'object {name}: hash mismatch')
    documents = json.loads(archive.read('tables/documents.json')) if 'documents' in manifest['tables'] else []
    missing = [d['id'] for d in documents if d.get('storage_name') not in manifest['objects']]
    if missing:
        problems.append(f'{len(missing)} document rows have no stored original in this backup')
    return manifest, problems


def restore(source):
    from sqlalchemy import text
    from api.migrate import upgrade
    from api.database import engine
    from api.datasync import all_tables, insert_rows, reset_sequences, row_counts
    from api.storage import get_storage, StorageError
    archive, manifest = _open(source)
    upgrade()  # target schema to head
    tables = all_tables()
    with engine.connect() as connection:
        occupied = {k: v for k, v in row_counts(connection, tables).items() if v}
    if occupied:
        raise SystemExit(f'Target database is not empty ({occupied}); restore refuses to merge or overwrite records')
    store = get_storage()
    with engine.begin() as connection:
        if connection.dialect.name == 'sqlite':
            connection.execute(text('PRAGMA foreign_keys=ON'))
        written = {}
        for table in tables:
            if table.name not in manifest['tables']:
                continue
            rows = json.loads(archive.read(f'tables/{table.name}.json'))
            written[table.name] = insert_rows(connection, table, rows)
        reset_sequences(connection, tables)
    uploaded = skipped = 0
    for name in manifest['objects']:
        if store.exists(name):
            skipped += 1
            continue
        try:
            store.write(name, archive.read(f'objects/{name}'))
            uploaded += 1
        except StorageError as exc:
            raise SystemExit(f'Object upload failed for one original: {exc}; database rows were restored, rerun restore of objects after fixing storage')
    if manifest.get('includes_app_key') and not _app_key_bytes():
        from api.config import DATA_DIR
        path = DATA_DIR / 'file_crypt.key'
        path.write_bytes(archive.read('app_key'))
        print('Application key restored to', path)
    return {'tables': written, 'objects_uploaded': uploaded, 'objects_already_present': skipped}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('operation', choices=['create', 'verify', 'restore'])
    ap.add_argument('path')
    ap.add_argument('--include-app-key', action='store_true', help='Also store the application encryption key inside the (backup-key encrypted) archive')
    args = ap.parse_args()
    if args.operation == 'create':
        target, manifest = create(args.path, args.include_app_key)
        print(f'Backup written: {target}')
        print(f"  database {manifest['database']} schema {manifest['alembic_version']}; rows per table: {json.dumps(manifest['tables'])}")
        print(f"  originals: {len(manifest['objects'])} encrypted objects; app key included: {manifest['includes_app_key']}")
        print('  Store BABYCARE_BACKUP_KEY and the application key separately from this file.')
    elif args.operation == 'verify':
        manifest, problems = verify(args.path)
        print(f"Backup from {manifest['created_at']} ({manifest['database']}, schema {manifest['alembic_version']}): {sum(manifest['tables'].values())} rows, {len(manifest['objects'])} originals")
        if problems:
            print('PROBLEMS:')
            for p in problems:
                print('  -', p)
            sys.exit(2)
        print('Integrity OK')
    else:
        result = restore(args.path)
        print('Restore complete:', json.dumps(result))
        print('Start the API against this database and sign in to confirm records open; then run a fresh backup.')


if __name__ == '__main__':
    main()
