"""新增数据库切换完成标记，供启动阶段校验目标库。"""

import sqlalchemy as sa
from alembic import op


revision = "b3c4d5e6f7a8"
down_revision = "a2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    tables = {name.upper() for name in sa.inspect(bind).get_table_names()}
    if "DATABASE_MIGRATION_MARKERS" not in tables:
        op.create_table(
            "DATABASE_MIGRATION_MARKERS",
            sa.Column("ID", sa.String(64), primary_key=True),
            sa.Column("STATUS", sa.String(32), nullable=False),
            sa.Column("SOURCE_FINGERPRINT", sa.String(64), nullable=False),
            sa.Column("TARGET_FINGERPRINT", sa.String(64), nullable=False),
            sa.Column("COMPLETED_AT", sa.Text(), nullable=False),
        )


def downgrade():
    bind = op.get_bind()
    tables = {name.upper() for name in sa.inspect(bind).get_table_names()}
    if "DATABASE_MIGRATION_MARKERS" in tables:
        op.drop_table("DATABASE_MIGRATION_MARKERS")
