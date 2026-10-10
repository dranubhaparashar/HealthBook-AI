"""Document import provenance and medication course types (additive only)."""
from alembic import op
import sqlalchemy as sa

revision='0004'
down_revision='0003'

def upgrade():
    with op.batch_alter_table('documents') as batch:
        batch.add_column(sa.Column('source_ref',sa.String(40),nullable=True))
        batch.add_column(sa.Column('import_batch',sa.String(80),nullable=True))
        batch.add_column(sa.Column('notes',sa.Text(),nullable=False,server_default=''))
    op.create_index('ix_documents_source_ref','documents',['source_ref'])
    with op.batch_alter_table('medicines') as batch:
        batch.add_column(sa.Column('course_type',sa.String(30),nullable=False,server_default='prescription'))

def downgrade():
    raise RuntimeError('Destructive rollback disabled; retain history or restore an encrypted backup')
