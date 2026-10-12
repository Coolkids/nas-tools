"""为媒体索引添加可发布的同步代次。"""

import time

import sqlalchemy as sa
from alembic import op


revision = "f2a3b4c5d6e7"
down_revision = "b3c4d5e6f7a8"
branch_labels = None
depends_on = None

_LEGACY_GENERATION = "legacy"


def _table_name(bind, expected):
    names = {name.upper(): name for name in sa.inspect(bind).get_table_names()}
    return names.get(expected.upper())


def _column_names(bind, table):
    return {column["name"].upper() for column in sa.inspect(bind).get_columns(table)}


def _index_names(bind, table):
    return {index["name"] for index in sa.inspect(bind).get_indexes(table)}


def _ensure_column(bind, table, column):
    if column.name.upper() not in _column_names(bind, table):
        op.add_column(table, column)


def _deduplicate_statistics(bind, table):
    rows = bind.execute(sa.text(
        f"SELECT ID, SERVER FROM {table} WHERE SERVER IS NOT NULL ORDER BY ID"
    )).mappings().all()
    latest_by_server = {}
    duplicates = []
    for row in rows:
        server = row["SERVER"]
        if server in latest_by_server:
            duplicates.append(latest_by_server[server])
        latest_by_server[server] = row["ID"]
    for identifier in duplicates:
        bind.execute(sa.text(f"DELETE FROM {table} WHERE ID = :id"), {"id": identifier})


def _seed_missing_statistics(bind, items_table, stats_table):
    counts = bind.execute(sa.text(f"""
        SELECT SERVER, COUNT(*) AS TOTAL_COUNT,
               SUM(CASE WHEN lower(ITEM_TYPE) IN ('movie') THEN 1 ELSE 0 END) AS MOVIE_COUNT,
               SUM(CASE WHEN lower(ITEM_TYPE) IN ('series', 'show') THEN 1 ELSE 0 END) AS TV_COUNT
        FROM {items_table} WHERE SERVER IS NOT NULL GROUP BY SERVER
    """)).mappings().all()
    known_servers = set(bind.execute(sa.text(
        f"SELECT SERVER FROM {stats_table} WHERE SERVER IS NOT NULL"
    )).scalars())
    update_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    for row in counts:
        if row["SERVER"] in known_servers:
            continue
        bind.execute(sa.text(f"""
            INSERT INTO {stats_table}
                (SERVER, ACTIVE_GENERATION, TOTAL_COUNT, MOVIE_COUNT, TV_COUNT, UPDATE_TIME)
            VALUES (:server, :generation, :total, :movies, :series, :updated)
        """), {
            "server": row["SERVER"],
            "generation": _LEGACY_GENERATION,
            "total": str(row["TOTAL_COUNT"] or 0),
            "movies": str(row["MOVIE_COUNT"] or 0),
            "series": str(row["TV_COUNT"] or 0),
            "updated": update_time,
        })


def _ensure_index(bind, name, table, columns, unique=False):
    if name not in _index_names(bind, table):
        op.create_index(name, table, columns, unique=unique)


def upgrade():
    bind = op.get_bind()
    items_table = _table_name(bind, "MEDIASYNC_ITEMS")
    stats_table = _table_name(bind, "MEDIASYNC_STATISTICS")
    if not items_table or not stats_table:
        raise RuntimeError("媒体同步代次升级缺少媒体索引或统计表")

    _ensure_column(bind, items_table, sa.Column(
        "GENERATION_ID", sa.String(64), nullable=False,
        server_default=sa.text("'legacy'")))
    _ensure_column(bind, stats_table, sa.Column(
        "ACTIVE_GENERATION", sa.String(64), nullable=False,
        server_default=sa.text("'legacy'")))
    bind.execute(sa.text(
        f"UPDATE {items_table} SET GENERATION_ID = :generation WHERE GENERATION_ID IS NULL"
    ), {"generation": _LEGACY_GENERATION})
    bind.execute(sa.text(
        f"UPDATE {stats_table} SET ACTIVE_GENERATION = :generation "
        "WHERE ACTIVE_GENERATION IS NULL"
    ), {"generation": _LEGACY_GENERATION})

    _deduplicate_statistics(bind, stats_table)
    _seed_missing_statistics(bind, items_table, stats_table)

    item_indexes = _index_names(bind, items_table)
    old_item_index = "UN_INDX_MEDIASYNC_ITEMS_SERVER_ITEM"
    if old_item_index in item_indexes:
        op.drop_index(old_item_index, table_name=items_table)
    for index_name in _index_names(bind, stats_table):
        if index_name.lower() == "ix_mediasync_statistics_server":
            op.drop_index(index_name, table_name=stats_table)
    _ensure_index(bind, "UN_INDX_MEDIASYNC_ITEMS_SERVER_GENERATION_ITEM", items_table,
                  ["SERVER", "GENERATION_ID", "ITEM_ID"], unique=True)
    _ensure_index(bind, "UN_INDX_MEDIASYNC_STATISTICS_SERVER", stats_table,
                  ["SERVER"], unique=True)


def downgrade():
    bind = op.get_bind()
    items_table = _table_name(bind, "MEDIASYNC_ITEMS")
    stats_table = _table_name(bind, "MEDIASYNC_STATISTICS")
    if items_table:
        indexes = _index_names(bind, items_table)
        if "UN_INDX_MEDIASYNC_ITEMS_SERVER_GENERATION_ITEM" in indexes:
            op.drop_index("UN_INDX_MEDIASYNC_ITEMS_SERVER_GENERATION_ITEM", table_name=items_table)
        if "UN_INDX_MEDIASYNC_ITEMS_SERVER_ITEM" not in indexes:
            op.create_index("UN_INDX_MEDIASYNC_ITEMS_SERVER_ITEM", items_table,
                            ["SERVER", "ITEM_ID"], unique=True)
        if "GENERATION_ID" in _column_names(bind, items_table):
            op.drop_column(items_table, "GENERATION_ID")
    if stats_table:
        indexes = _index_names(bind, stats_table)
        if "UN_INDX_MEDIASYNC_STATISTICS_SERVER" in indexes:
            op.drop_index("UN_INDX_MEDIASYNC_STATISTICS_SERVER", table_name=stats_table)
        if "ACTIVE_GENERATION" in _column_names(bind, stats_table):
            op.drop_column(stats_table, "ACTIVE_GENERATION")
