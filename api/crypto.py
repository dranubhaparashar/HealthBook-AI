import os
from cryptography.fernet import Fernet
from sqlalchemy.types import TypeDecorator, Text
from .config import DATA_DIR

def crypto():
    key = os.getenv('BABYCARE_FERNET_KEY')
    if key:
        return Fernet(key.encode())
    path = DATA_DIR / 'file_crypt.key'
    if not path.exists():
        # Losing a key must not silently create a new key alongside unreadable records.
        existing_files=any((DATA_DIR/'encrypted_files').glob('*.enc')) or any(DATA_DIR.glob('before-migration-*.enc'))
        from .config import DATABASE_URL
        encrypted_rows=False
        if DATABASE_URL.startswith('sqlite:'):
            import sqlite3
            from sqlalchemy.engine import make_url
            dbpath=make_url(DATABASE_URL).database
            if dbpath and os.path.exists(dbpath):
                with sqlite3.connect(dbpath) as connection:
                    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='patients'").fetchone():
                        encrypted_rows=bool(connection.execute("SELECT 1 FROM patients WHERE substr(name,1,4)='enc:' LIMIT 1").fetchone())
        if existing_files or encrypted_rows:
            raise RuntimeError('Existing encrypted records have no application key. Restore the original key; no replacement key was generated.')
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(Fernet.generate_key())
        except FileExistsError:
            pass
    return Fernet(path.read_bytes())

def encrypt(raw: bytes):
    return crypto().encrypt(raw)

def decrypt(raw: bytes):
    return crypto().decrypt(raw)

class EncryptedText(TypeDecorator):
    """Application encryption for clinical text; dates/IDs still need disk encryption."""
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else 'enc:' + encrypt(str(value).encode()).decode()

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.startswith('enc:'):
            return decrypt(value[4:].encode()).decode()
        return value  # Legacy values are encrypted by migration 0002.
