"""Dialect-neutral raw table copy used by the cloud migration and the local backup/restore tools.

Rows are moved exactly as stored: application-encrypted columns (the 'enc:' strings) are copied without
decryption, so the Fernet key is never needed here and never embedded in a backup by this module.
"""
import base64
from datetime import date, datetime
from sqlalchemy import text, Boolean, Date, DateTime, Integer, Float, LargeBinary
from sqlalchemy.types import TypeDecorator


def all_tables():
    """Every mapped table in foreign-key order (imports the model modules so metadata is complete)."""
    from .database import Base
    from . import models  # noqa: F401
    for optional in ('lifetime_models', 'share_models', 'care_models'):
        try:
            __import__(f'{__package__}.{optional}')
        except ImportError:
            pass
    return [t for t in Base.metadata.sorted_tables if t.name != 'alembic_version']


def _base_type(column):
    kind = column.type
    while isinstance(kind, TypeDecorator):
        kind = kind.impl
    return kind


def jsonable(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return 'b64:' + base64.b64encode(bytes(value)).decode()
    return value


def dump_rows(connection, table):
    """Plain dicts with JSON-safe scalars, ordered by primary key when there is one."""
    order = ', '.join(f'"{c.name}"' for c in table.primary_key.columns) or '1'
    result = connection.execute(text(f'SELECT * FROM "{table.name}" ORDER BY {order}'))
    return [{k: jsonable(v) for k, v in row._mapping.items()} for row in result]


def coerce(column, value):
    """Convert a JSON/SQLite scalar to the Python type the target column expects."""
    if value is None:
        return None
    kind = _base_type(column)
    if isinstance(kind, Boolean):
        if isinstance(value, str):
            return value.strip().lower() in ('1', 'true', 't', 'yes')
        return bool(value)
    if isinstance(kind, DateTime):
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if isinstance(kind, Date):
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        return date.fromisoformat(str(value)[:10])
    if isinstance(kind, Integer) and isinstance(value, str) and value.lstrip('-').isdigit():
        return int(value)
    if isinstance(kind, Float) and isinstance(value, str):
        return float(value)
    if isinstance(kind, LargeBinary) and isinstance(value, str) and value.startswith('b64:'):
        return base64.b64decode(value[4:])
    return value


def insert_rows(connection, table, rows, batch=200):
    """Insert raw rows bypassing ORM type decorators; returns the number of rows written."""
    if not rows:
        return 0
    columns = [c for c in table.columns if c.name in rows[0]]
    names = ', '.join(f'"{c.name}"' for c in columns)
    params = ', '.join(f':{c.name}' for c in columns)
    statement = text(f'INSERT INTO "{table.name}" ({names}) VALUES ({params})')
    written = 0
    for start in range(0, len(rows), batch):
        chunk = [{c.name: coerce(c, row.get(c.name)) for c in columns} for row in rows[start:start + batch]]
        connection.execute(statement, chunk)
        written += len(chunk)
    return written


def reset_sequences(connection, tables):
    """After inserting explicit ids into PostgreSQL, move each serial sequence past the highest id."""
    if connection.dialect.name != 'postgresql':
        return
    for table in tables:
        pk = [c for c in table.primary_key.columns]
        if len(pk) != 1 or not isinstance(_base_type(pk[0]), Integer) or not pk[0].autoincrement:
            continue
        name = pk[0].name
        connection.execute(text(f"""SELECT setval(pg_get_serial_sequence('"{table.name}"', '{name}'), COALESCE(MAX("{name}"), 1), MAX("{name}") IS NOT NULL) FROM "{table.name}" """))


def row_counts(connection, tables):
    return {t.name: connection.execute(text(f'SELECT COUNT(*) FROM "{t.name}"')).scalar() for t in tables}
