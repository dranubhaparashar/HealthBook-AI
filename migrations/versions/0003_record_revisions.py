"""Preserve previous appointment, vaccine, admission and profile values."""
from alembic import op
import sqlalchemy as sa

revision='0003'
down_revision='0002'

def upgrade():
    op.create_table('record_revisions',
        sa.Column('id',sa.Integer(),primary_key=True),
        sa.Column('patient_id',sa.Integer(),sa.ForeignKey('patients.id',ondelete='CASCADE'),nullable=False),
        sa.Column('user_id',sa.Integer(),sa.ForeignKey('users.id',ondelete='SET NULL'),nullable=True),
        sa.Column('entity',sa.String(55),nullable=False),
        sa.Column('entity_id',sa.Integer(),nullable=False),
        sa.Column('snapshot_json',sa.Text(),nullable=False),
        sa.Column('changed_at',sa.DateTime(timezone=True),nullable=False))
    op.create_index('ix_record_revisions_patient_id','record_revisions',['patient_id'])

def downgrade():
    raise RuntimeError('Destructive rollback disabled; retain history or restore an encrypted backup')
