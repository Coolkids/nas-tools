"""校验历史值后，为业务标识符设置明确长度。"""

import sqlalchemy as sa
from alembic import op


revision = "f7a8b9c0d1e2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


IDENTIFIER_LENGTHS = {
    "DOWNLOAD_HISTORY": {"TMDBID": 64},
    "RSS_HISTORY": {"RSSID": 128, "TMDBID": 64},
    "RSS_MOVIES": {"TMDBID": 64},
    "RSS_TVS": {"TMDBID": 64},
    "RSS_TV_EPISODES": {"RSSID": 128},
    "SEARCH_RESULT_INFO": {"TMDBID": 64},
    "RECOGNITION_ATTEMPT": {
        "REQUEST_ID": 128,
        "ATTEMPT_ID": 128,
        "PROVIDER_ID": 128,
    },
    "USERRSS_TASK_HISTORY": {"TASK_ID": 128},
    "MEDIASYNC_ITEMS": {"TMDBID": 64, "IMDBID": 32},
}


def _actual_table_names(bind):
    return {name.casefold(): name for name in sa.inspect(bind).get_table_names()}


def upgrade():
    bind = op.get_bind()
    tables = _actual_table_names(bind)
    pending = []
    # 先检查所有现存列，再统一修改结构，避免错误数据导致静默截断。
    for requested_table, requested_columns in IDENTIFIER_LENGTHS.items():
        actual_table = tables.get(requested_table.casefold())
        if actual_table is None:
            continue
        columns = {column["name"].casefold(): column
                   for column in sa.inspect(bind).get_columns(actual_table)}
        reflected = sa.Table(actual_table, sa.MetaData(), autoload_with=bind)
        keys = list(reflected.primary_key.columns) or [next(iter(reflected.columns))]
        for requested_column, limit in requested_columns.items():
            column_info = columns.get(requested_column.casefold())
            if column_info is None:
                raise RuntimeError(
                    f"结构升级表 {actual_table} 缺少 {requested_column}，拒绝继续")
            column = next(item for item in reflected.columns
                          if item.name.casefold() == requested_column.casefold())
            length = sa.func.length(column.cast(sa.String()))
            statement = sa.select(*keys, column, length).where(
                column.is_not(None), length > limit)
            cursor = bind.execute(statement)
            try:
                while True:
                    rows = cursor.fetchmany(500)
                    if not rows:
                        break
                    for row in rows:
                        key = ", ".join(
                            f"{item.name}={row[index]!r}"
                            for index, item in enumerate(keys))
                        raise RuntimeError(
                            f"{actual_table}.{column.name} 记录 ({key}) 长度为 "
                            f"{row[-1]}，超过 {limit} 个字符；拒绝截断，请先修复或导出该记录")
            finally:
                cursor.close()
            target_type = sa.String(limit)
            if (not isinstance(column_info["type"], sa.String)
                    or column_info["type"].length != limit):
                pending.append((actual_table, column_info, target_type, None))

    search_table = tables.get("search_result_info")
    if search_table is not None:
        columns = {column["name"].casefold(): column
                   for column in sa.inspect(bind).get_columns(search_table)}
        size = columns.get("size")
        if size is not None and not isinstance(size["type"], sa.Text):
            pending.append((search_table, size, sa.Text(), '"SIZE"::text'))

    by_table = {}
    for table, column_info, target_type, postgresql_using in pending:
        by_table.setdefault(table, []).append(
            (column_info, target_type, postgresql_using))
    for table, changes in by_table.items():
        with op.batch_alter_table(table) as batch:
            for column_info, target_type, postgresql_using in changes:
                options = {}
                if postgresql_using and bind.dialect.name == "postgresql":
                    options["postgresql_using"] = postgresql_using
                batch.alter_column(
                    column_info["name"],
                    existing_type=column_info["type"],
                    existing_nullable=column_info["nullable"],
                    existing_server_default=column_info.get("default"),
                    type_=target_type,
                    **options,
                )


def downgrade():
    raise RuntimeError("业务标识符长度升级不可自动降级")
