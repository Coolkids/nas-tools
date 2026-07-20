"""add search result indexes

Revision ID: 8f2a1b3c4d5e
Revises: 720a6289a697
Create Date: 2024-01-15 10:00:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = '8f2a1b3c4d5e'
down_revision = '720a6289a697'
branch_labels = None
depends_on = None


def upgrade() -> None:
    try:
        with op.batch_alter_table("SEARCH_RESULT_INFO") as batch_op:
            batch_op.create_index('idx_tmdbid', ['TMDBID'])
            batch_op.create_index('idx_site', ['SITE'])
            batch_op.create_index('idx_torrent_name', ['TORRENT_NAME'])
            batch_op.create_index('idx_type', ['TYPE'])
    except Exception as e:
        print(str(e))


def downgrade() -> None:
    try:
        with op.batch_alter_table("SEARCH_RESULT_INFO") as batch_op:
            batch_op.drop_index('idx_tmdbid')
            batch_op.drop_index('idx_site')
            batch_op.drop_index('idx_torrent_name')
            batch_op.drop_index('idx_type')
    except Exception as e:
        print(str(e))
