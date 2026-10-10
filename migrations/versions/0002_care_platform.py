"""Add care workflows and encrypt legacy clinical text in place."""
from alembic import op
from sqlalchemy import inspect, Column, Text, select, update
from migrations.v2_schema import Base
from api.crypto import EncryptedText
revision = '0002'
down_revision = '0001'

def upgrade():
    bind = op.get_bind()
    existing = set(inspect(bind).get_table_names())
    # Nullable references added after tables exist; application validates child scope.
    Base.metadata.create_all(bind, checkfirst=True)
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            continue
        columns = {c['name']: c for c in inspect(bind).get_columns(table.name)}
        for column in table.columns:
            if column.name not in columns:
                if column.foreign_keys:
                    fk=next(iter(column.foreign_keys))
                    target=fk.column.table.name
                    op.execute(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" INTEGER REFERENCES "{target}" ("id") ON DELETE {fk.ondelete or "NO ACTION"}')
                else:
                    op.add_column(table.name, Column(column.name, column.type, nullable=column.nullable, server_default=column.server_default))
        if bind.dialect.name == 'postgresql':
            for column in table.columns:
                if isinstance(column.type, EncryptedText) and column.name in columns:
                    op.alter_column(table.name, column.name, type_=Text())
        encrypted = [c for c in table.columns if isinstance(c.type, EncryptedText)]
        if encrypted:
            # Select through the type decorator, then bind encrypted values back.
            for row in bind.execute(select(table)).mappings().all():
                bind.execute(update(table).where(table.c.id == row['id']).values(**{c.name:row[c.name] for c in encrypted}))

def downgrade():
    raise RuntimeError('Restore an encrypted backup; this migration must not decrypt records')
