"""add search task table and keyword column

Revision ID: 9a1b2c3d4e5f
Revises: 8f2a1b3c4d5e
Create Date: 2024-06-01 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9a1b2c3d4e5f'
down_revision = '8f2a1b3c4d5e'
branch_labels = None
depends_on = None


def upgrade() -> None:
    try:
        op.create_table('SEARCH_TASK',
            sa.Column('ID', sa.Integer, primary_key=True),
            sa.Column('KEYWORD', sa.Text, unique=True, index=True),
            sa.Column('STATUS', sa.Text),
            sa.Column('START_TIME', sa.Text),
            sa.Column('END_TIME', sa.Text),
            sa.Column('MESSAGE', sa.Text)
        )
    except Exception as e:
        print(str(e))
    try:
        with op.batch_alter_table("SEARCH_RESULT_INFO") as batch_op:
            batch_op.add_column(sa.Column('KEYWORD', sa.Text))
            batch_op.create_index('idx_keyword', ['KEYWORD'])
    except Exception as e:
        print(str(e))


def downgrade() -> None:
    try:
        with op.batch_alter_table("SEARCH_RESULT_INFO") as batch_op:
            batch_op.drop_index('idx_keyword')
            batch_op.drop_column('KEYWORD')
    except Exception as e:
        print(str(e))
    try:
        op.drop_table('SEARCH_TASK')
    except Exception as e:
        print(str(e))
