"""Intelligent stool-photo intake: capture-time provenance on photos, suggestion/confirmation audit trail on stool
entries, and a pending-intake table for batch uploads awaiting parent confirmation. Additive only."""
from alembic import op
import sqlalchemy as sa

revision='0008'
down_revision='0007'

def upgrade():
    with op.batch_alter_table('stool_photos') as b:
        b.add_column(sa.Column('detected_taken_at',sa.DateTime(timezone=True),nullable=True))
        b.add_column(sa.Column('taken_at_source',sa.String(20),nullable=False,server_default=''))
        b.add_column(sa.Column('taken_at_confidence',sa.String(10),nullable=False,server_default=''))
        b.add_column(sa.Column('taken_at_tz',sa.String(40),nullable=False,server_default=''))
        b.add_column(sa.Column('original_filename',sa.Text(),nullable=False,server_default=''))
        b.add_column(sa.Column('metadata_json',sa.Text(),nullable=False,server_default=''))
    with op.batch_alter_table('stool_entries') as b:
        b.add_column(sa.Column('time_source',sa.String(20),nullable=False,server_default='manual'))
        b.add_column(sa.Column('ai_suggestion_json',sa.Text(),nullable=False,server_default=''))
        b.add_column(sa.Column('parent_values_json',sa.Text(),nullable=False,server_default=''))
    op.create_table('stool_intake',
        sa.Column('id',sa.Integer(),primary_key=True),
        sa.Column('patient_id',sa.Integer(),sa.ForeignKey('patients.id',ondelete='CASCADE'),nullable=False,index=True),
        sa.Column('user_id',sa.Integer(),sa.ForeignKey('users.id',ondelete='SET NULL'),nullable=True),
        sa.Column('storage_name',sa.String(70),nullable=False,unique=True),
        sa.Column('sha256',sa.String(64),nullable=False,index=True),
        sa.Column('mime',sa.String(40),nullable=False),
        sa.Column('width',sa.Integer(),nullable=False,server_default='0'),
        sa.Column('height',sa.Integer(),nullable=False,server_default='0'),
        sa.Column('original_filename',sa.Text(),nullable=False,server_default=''),
        sa.Column('detected_at',sa.DateTime(timezone=True),nullable=True),
        sa.Column('detected_source',sa.String(20),nullable=False,server_default='none'),
        sa.Column('detected_confidence',sa.String(10),nullable=False,server_default='none'),
        sa.Column('detected_tz',sa.String(40),nullable=False,server_default=''),
        sa.Column('detection_json',sa.Text(),nullable=False,server_default=''),
        sa.Column('suggestion_json',sa.Text(),nullable=False,server_default=''),
        sa.Column('status',sa.String(12),nullable=False,server_default='pending'),
        sa.Column('duplicate_of_entry_id',sa.Integer(),sa.ForeignKey('stool_entries.id',ondelete='SET NULL'),nullable=True),
        sa.Column('stool_entry_id',sa.Integer(),sa.ForeignKey('stool_entries.id',ondelete='SET NULL'),nullable=True),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('expires_at',sa.DateTime(timezone=True),nullable=True))

def downgrade():
    raise RuntimeError('Destructive rollback disabled; retain history or restore an encrypted backup')
