"""识别记录列表使用复合索引稳定分页并减少排序。"""

import sqlalchemy as sa
from alembic import op


revision = "f8b9c0d1e2f3"
down_revision = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None


_TABLE = "RECOGNITION_REQUEST"
_INDEXES = (
    ("IDX_RECOG_CREATED_REQUEST", ("CREATED_AT", "REQUEST_ID")),
    ("IDX_RECOG_SOURCE_CREATED_REQUEST", ("SOURCE", "CREATED_AT", "REQUEST_ID")),
)
_OLD_INDEXES = (
    "ix_recognition_request_created_at",
    "ix_recognition_request_source",
)


def _index_names(bind):
    return {index["name"].casefold(): index["name"]
            for index in sa.inspect(bind).get_indexes(_TABLE)}


def _create_index(name, columns, dialect):
    options = {}
    if dialect in {"mysql", "mariadb"}:
        prefixes = {column: 191 for column in columns if column in {"SOURCE", "CREATED_AT"}}
        if prefixes:
            options["mysql_length"] = prefixes
            options["mariadb_length"] = prefixes
    op.create_index(name, _TABLE, list(columns), **options)


def upgrade():
    bind = op.get_bind()
    tables = {name.casefold() for name in sa.inspect(bind).get_table_names()}
    if _TABLE.casefold() not in tables:
        # The media-only legacy snapshot has no recognition domain.
        return

    # Text columns can contain arbitrary legacy values. The existing portable
    # metadata intentionally omits their indexes on PostgreSQL to avoid btree
    # entry-size failures; preserve that policy here as well.
    if bind.dialect.name == "postgresql":
        return

    columns = {column["name"].casefold()
               for column in sa.inspect(bind).get_columns(_TABLE)}
    required = {"created_at", "request_id", "source"}
    if not required <= columns:
        raise RuntimeError("识别记录表缺少分页索引所需字段，拒绝继续升级")

    indexes = _index_names(bind)
    for name, index_columns in _INDEXES:
        if name.casefold() not in indexes:
            _create_index(name, index_columns, bind.dialect.name)
    indexes = _index_names(bind)
    for old_name in _OLD_INDEXES:
        actual_name = indexes.get(old_name.casefold())
        if actual_name:
            op.drop_index(actual_name, table_name=_TABLE)


def downgrade():
    bind = op.get_bind()
    tables = {name.casefold() for name in sa.inspect(bind).get_table_names()}
    if _TABLE.casefold() not in tables or bind.dialect.name == "postgresql":
        return

    indexes = _index_names(bind)
    for name, _ in _INDEXES:
        actual_name = indexes.get(name.casefold())
        if actual_name:
            op.drop_index(actual_name, table_name=_TABLE)
    indexes = _index_names(bind)
    for name, column in zip(_OLD_INDEXES, ("CREATED_AT", "SOURCE")):
        if name.casefold() not in indexes:
            _create_index(name, (column,), bind.dialect.name)
