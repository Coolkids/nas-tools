"""使用确定性摘要键替换不便跨库索引的长文本唯一键。"""

import hashlib

import sqlalchemy as sa
from alembic import op


revision = "a2b3c4d5e6f7"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def _digest(values):
    if any(value is None for value in values):
        return None
    result = hashlib.sha256()
    for value in values:
        encoded = str(value).encode("utf-8", "surrogatepass")
        result.update(len(encoded).to_bytes(8, "big"))
        result.update(encoded)
    return result.hexdigest()


def _add_column_if_missing(bind, table, name):
    columns = {column["name"].upper() for column in sa.inspect(bind).get_columns(table)}
    if name.upper() not in columns:
        op.add_column(table, sa.Column(name, sa.String(64), nullable=True))


def _backfill(bind, table, key_columns, digest_column):
    last_id = None
    while True:
        where = "" if last_id is None else "WHERE ID > :last_id"
        values = {} if last_id is None else {"last_id": last_id}
        rows = bind.execute(sa.text(
            f"SELECT ID, {', '.join(key_columns)} FROM {table} {where} "
            "ORDER BY ID LIMIT 500"), values).mappings().all()
        if not rows:
            return
        for row in rows:
            digest = _digest(tuple(row[column] for column in key_columns))
            if digest is not None:
                bind.execute(sa.text(
                    f"UPDATE {table} SET {digest_column} = :digest WHERE ID = :row_id"),
                    {"digest": digest, "row_id": row["ID"]})
        last_id = rows[-1]["ID"]


def _verify_hashes(bind, table, key_columns, digest_column):
    duplicates = bind.execute(sa.text(
        f"SELECT {digest_column} FROM {table} WHERE {digest_column} IS NOT NULL "
        f"GROUP BY {digest_column} HAVING COUNT(*) > 1")).scalars().all()
    for digest in duplicates:
        rows = bind.execute(sa.text(
            f"SELECT {', '.join(key_columns)} FROM {table} "
            f"WHERE {digest_column} = :digest"), {"digest": digest}).all()
        if len({tuple(row) for row in rows}) > 1:
            raise RuntimeError(f"{table} 摘要唯一键发生碰撞，拒绝升级")


def _drop_index_if_present(bind, table, name):
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes(table)}
    if name in indexes:
        op.drop_index(name, table_name=table)


def _create_unique_index_if_missing(bind, name, table, column):
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes(table)}
    if name not in indexes:
        op.create_index(name, table, [column], unique=True)


def upgrade():
    bind = op.get_bind()
    for table, key_columns, digest_column in (
            ("RSS_TORRENTS", ("ENCLOSURE",), "ENCLOSURE_HASH"),
            ("SYNC_HISTORY", ("PATH", "DEST"), "PATH_DEST_HASH"),
            ("SEARCH_TASK", ("KEYWORD",), "KEYWORD_HASH")):
        tables = {name.upper() for name in sa.inspect(bind).get_table_names()}
        if table not in tables:
            raise RuntimeError(f"数据库缺少结构升级所需表：{table}")
        _add_column_if_missing(bind, table, digest_column)
        _backfill(bind, table, key_columns, digest_column)
        _verify_hashes(bind, table, key_columns, digest_column)

    _drop_index_if_present(bind, "RSS_TORRENTS", "UN_INDX_RSS_TORRENTS_ENCLOSURE")
    _drop_index_if_present(bind, "SYNC_HISTORY", "UN_INDX_SYNC_HISTORY_PATH_DEST")
    _create_unique_index_if_missing(
        bind, "UN_INDX_RSS_TORRENTS_ENCLOSURE", "RSS_TORRENTS", "ENCLOSURE_HASH")
    _create_unique_index_if_missing(
        bind, "UN_INDX_SYNC_HISTORY_PATH_DEST", "SYNC_HISTORY", "PATH_DEST_HASH")
    _create_unique_index_if_missing(
        bind, "UN_INDX_SEARCH_TASK_KEYWORD_HASH", "SEARCH_TASK", "KEYWORD_HASH")


def downgrade():
    # 长文本唯一索引无法在 MySQL/MariaDB 上安全回退为全文唯一索引。
    raise RuntimeError("不可自动回退摘要唯一键结构；请先导出并通过受控迁移恢复")
