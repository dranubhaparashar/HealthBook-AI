"""Adopt existing starter tables without losing records."""
from alembic import op
from migrations.legacy_schema import Base
revision = '0001'
down_revision = None

def upgrade():
    Base.metadata.create_all(op.get_bind(), checkfirst=True)

def downgrade():
    raise RuntimeError('Destructive schema rollback disabled; restore an encrypted backup')
