"""Family workspace profiles (sex, relationship, per-profile modules) and the vitals journal for adult profiles.

Revision ID: 0009
Revises: 0008
"""
from alembic import op
import sqlalchemy as sa

revision='0009'
down_revision='0008'
branch_labels=None
depends_on=None

def upgrade():
    with op.batch_alter_table('patients') as b:
        b.add_column(sa.Column('sex',sa.String(16),nullable=True))
        b.add_column(sa.Column('relationship',sa.String(32),nullable=True))
        b.add_column(sa.Column('modules_json',sa.Text(),nullable=True))
    op.create_table('vitals_entries',
        sa.Column('id',sa.Integer(),primary_key=True),
        sa.Column('patient_id',sa.Integer(),sa.ForeignKey('patients.id',ondelete='CASCADE'),nullable=False,index=True),
        sa.Column('recorded_by',sa.Integer(),sa.ForeignKey('users.id',ondelete='SET NULL'),nullable=True),
        sa.Column('at',sa.DateTime(timezone=True),nullable=False,index=True),
        sa.Column('local_date',sa.Date(),nullable=False,index=True),
        sa.Column('note',sa.Text(),nullable=False,server_default=''),
        sa.Column('source',sa.String(20),nullable=False,server_default='parent'),
        sa.Column('status',sa.String(12),nullable=False,server_default='active'),
        sa.Column('version',sa.Integer(),nullable=False,server_default='1'),
        sa.Column('client_key',sa.String(80),nullable=True),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('systolic',sa.Integer(),nullable=True),
        sa.Column('diastolic',sa.Integer(),nullable=True),
        sa.Column('heart_rate',sa.Integer(),nullable=True),
        sa.Column('spo2',sa.Integer(),nullable=True),
        sa.Column('glucose_mg_dl',sa.Float(),nullable=True),
        sa.Column('glucose_context',sa.String(20),nullable=False,server_default=''),
        sa.Column('weight_kg',sa.Float(),nullable=True),
        sa.Column('temperature_c',sa.Float(),nullable=True),
        sa.Column('meal',sa.Text(),nullable=False,server_default=''),
        sa.UniqueConstraint('patient_id','client_key',name='uq_vitals_client_key'),
    )

def downgrade():
    op.drop_table('vitals_entries')
    with op.batch_alter_table('patients') as b:
        b.drop_column('modules_json');b.drop_column('relationship');b.drop_column('sex')
