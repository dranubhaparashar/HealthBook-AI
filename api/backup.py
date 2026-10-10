"""Encrypted local SQLite backups; restore only into a new empty destination."""
import argparse
import io
import os
import sqlite3
import zipfile
from pathlib import Path
from datetime import datetime,timezone
from cryptography.fernet import Fernet

def backup(destination):
    from .config import DATA_DIR,DATABASE_URL
    from .database import engine
    from .crypto import crypto
    key=os.getenv('BABYCARE_BACKUP_KEY')
    if not key:raise RuntimeError('Set a separate BABYCARE_BACKUP_KEY; keep it outside the backup location')
    if engine.dialect.name!='sqlite':raise RuntimeError('For PostgreSQL use the documented pg_dump + encrypted storage backup procedure')
    crypto() # Ensure an existing file encryption key can be opened.
    buf=io.BytesIO()
    with sqlite3.connect(engine.url.database) as source,sqlite3.connect(':memory:') as snapshot:
        source.backup(snapshot)
        with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('babycare.sqlite3',snapshot.serialize())
            archive.writestr('file_crypt.key',os.getenv('BABYCARE_FERNET_KEY') or (DATA_DIR/'file_crypt.key').read_text())
            for path in (DATA_DIR/'encrypted_files').glob('*.enc'):
                archive.writestr('encrypted_files/'+path.name,path.read_bytes())
    target=Path(destination).resolve();target.parent.mkdir(parents=True,exist_ok=True)
    with target.open('xb') as stream:stream.write(Fernet(key.encode()).encrypt(buf.getvalue()))
    return target

def restore(source,destination):
    key=os.getenv('BABYCARE_BACKUP_KEY')
    if not key:raise RuntimeError('Set BABYCARE_BACKUP_KEY')
    destination=Path(destination).resolve()
    if destination.exists():raise RuntimeError('Restore destination must not exist; existing records will never be overwritten')
    raw=Fernet(key.encode()).decrypt(Path(source).read_bytes())
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for item in archive.infolist():
            target=(destination/item.filename).resolve()
            if not target.is_relative_to(destination) or item.is_dir():raise RuntimeError('Invalid archive member')
            if item.file_size>1024*1024*1024:raise RuntimeError('Archive entry is too large')
        destination.mkdir(parents=True)
        for item in archive.infolist():
            target=(destination/item.filename).resolve();target.parent.mkdir(parents=True,exist_ok=True)
            with target.open('xb') as stream:stream.write(archive.read(item))
    return destination

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=['create','restore']);parser.add_argument('path');parser.add_argument('--destination')
    args=parser.parse_args()
    if args.operation=='create':result=backup(args.path)
    else:
        if not args.destination:parser.error('--destination is required for restore')
        result=restore(args.path,args.destination)
    print('Completed:',result)

if __name__=='__main__':main()
