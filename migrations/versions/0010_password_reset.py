"""Password reset tokens (hashed, single-use, expiring).

Revision ID: 0010
Revises: 0009
"""
from alembic import op
import sqlalchemy as sa

revision='0010'
down_revision='0009'
branch_labels=None
depends_on=None

def upgrade():
    op.create_table('password_reset_tokens',
        sa.Column('id',sa.Integer(),primary_key=True),
        sa.Column('user_id',sa.Integer(),sa.ForeignKey('users.id',ondelete='CASCADE'),nullable=False,index=True),
        sa.Column('token_hash',sa.String(64),nullable=False,unique=True),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('expires_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('used_at',sa.DateTime(timezone=True),nullable=True),
    )

def downgrade():
    op.drop_table('password_reset_tokens')
