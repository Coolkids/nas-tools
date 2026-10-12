"""Bound the indexed media server identifier without truncating legacy values."""

import sqlalchemy as sa
from alembic import op


revision = "f6a7b8c9d0e1"
down_revision = "f5a6b7c8d9e0"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    actual_name = next((name for name in inspector.get_table_names()
                        if name.casefold() == "mediasync_statistics"), None)
    if actual_name is None:
        return
    columns = {column["name"].casefold(): column for column in inspector.get_columns(actual_name)}
    server = columns.get("server")
    if server is None:
        raise RuntimeError("媒体统计表缺少 SERVER 字段，拒绝结构升级")
    reflected = sa.Table(actual_name, sa.MetaData(), autoload_with=bind)
    server_column = next(column for column in reflected.columns
                         if column.name.casefold() == "server")
    cursor = bind.execute(sa.select(server_column))
    try:
        while True:
            rows = cursor.fetchmany(500)
            if not rows:
                break
            for (value,) in rows:
                if value is not None and len(str(value)) > 128:
                    raise RuntimeError(
                        "媒体统计 SERVER 值超过 128 个字符，拒绝截断或迁移")
    finally:
        cursor.close()

    current_type = server.get("type")
    if isinstance(current_type, sa.String) and current_type.length == 128:
        return
    with op.batch_alter_table(actual_name) as batch:
        batch.alter_column("SERVER", existing_type=current_type,
                           type_=sa.String(128), existing_nullable=True)


def downgrade():
    raise RuntimeError("SERVER 字段长度升级不可自动降级")
