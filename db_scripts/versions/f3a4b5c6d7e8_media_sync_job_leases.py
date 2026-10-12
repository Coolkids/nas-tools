"""持久化每个媒体服务器的同步租约与待发布代次。"""

import sqlalchemy as sa
from alembic import op


revision = "f3a4b5c6d7e8"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    tables = {name.upper() for name in sa.inspect(bind).get_table_names()}
    table = "MEDIASYNC_SYNC_JOBS"
    if table not in tables:
        op.create_table(
            table,
            sa.Column("SERVER", sa.String(128), primary_key=True),
            sa.Column("JOB_ID", sa.String(64), nullable=False),
            sa.Column("GENERATION_ID", sa.String(64), nullable=False),
            sa.Column("LEASE_TOKEN", sa.BigInteger(), nullable=False),
            sa.Column("LEASE_UNTIL", sa.Float(), nullable=False),
            sa.Column("EXPECTED_ACTIVE_GENERATION", sa.String(64), nullable=True),
            sa.Column("STATUS", sa.String(32), nullable=False),
            sa.Column("CREATED_AT", sa.Float(), nullable=False),
            sa.Column("UPDATED_AT", sa.Float(), nullable=False),
        )
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes(table)}
    if "UN_INDX_MEDIASYNC_SYNC_JOBS_JOB_ID" not in indexes:
        op.create_index("UN_INDX_MEDIASYNC_SYNC_JOBS_JOB_ID", table, ["JOB_ID"], unique=True)


def downgrade():
    bind = op.get_bind()
    tables = {name.upper() for name in sa.inspect(bind).get_table_names()}
    if "MEDIASYNC_SYNC_JOBS" not in tables:
        return
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("MEDIASYNC_SYNC_JOBS")}
    if "UN_INDX_MEDIASYNC_SYNC_JOBS_JOB_ID" in indexes:
        op.drop_index("UN_INDX_MEDIASYNC_SYNC_JOBS_JOB_ID", table_name="MEDIASYNC_SYNC_JOBS")
    op.drop_table("MEDIASYNC_SYNC_JOBS")
