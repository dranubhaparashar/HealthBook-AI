"""Share with Doctor: expiring selective share packages and their access log (additive only)."""
from alembic import op
import sqlalchemy as sa

revision = '0006'
down_revision = '0005'


def upgrade():
    op.create_table('share_packages',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('patient_id', sa.Integer(), sa.ForeignKey('patients.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('label', sa.Text(), nullable=False, server_default=''),
        sa.Column('reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('scope_json', sa.Text(), nullable=False),
        sa.Column('token_hash', sa.String(64), nullable=False, unique=True, index=True),
        sa.Column('pin_salt', sa.String(64), nullable=True),
        sa.Column('pin_hash', sa.String(128), nullable=True),
        sa.Column('view_only', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('allow_download', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('include_summary', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('document_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column('access_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_accessed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_table('share_access_log',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('package_id', sa.Integer(), sa.ForeignKey('share_packages.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('event', sa.String(30), nullable=False),
        sa.Column('ip_hash', sa.String(64), nullable=False, server_default=''),
        sa.Column('detail', sa.Text(), nullable=False, server_default=''))


def downgrade():
    raise RuntimeError('Destructive rollback disabled; retain history or restore an encrypted backup')
