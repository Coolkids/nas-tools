"""Store media sync lease deadlines without epoch-time precision loss."""

import sqlalchemy as sa
from alembic import op


revision = "f9c0d1e2f3a4"
down_revision = "f8b9c0d1e2f3"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    actual_name = next((name for name in sa.inspect(bind).get_table_names()
                        if name.casefold() == "mediasync_sync_jobs"), None)
    if actual_name is None:
        return

    columns = {column["name"].casefold(): column
               for column in sa.inspect(bind).get_columns(actual_name)}
    lease_until = columns.get("lease_until")
    if lease_until is None:
        raise RuntimeError("媒体同步租约表缺少 LEASE_UNTIL，拒绝结构升级")

    current_type = lease_until["type"]
    type_name = str(current_type).upper()
    if "DOUBLE" in type_name or getattr(current_type, "precision", None) == 53:
        return

    with op.batch_alter_table(actual_name) as batch:
        batch.alter_column(
            lease_until["name"], existing_type=current_type,
            type_=sa.Float(precision=53), existing_nullable=False)


def downgrade():
    raise RuntimeError("媒体同步租约时间精度升级不可自动降级")
