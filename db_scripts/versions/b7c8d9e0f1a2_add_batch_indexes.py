"""为批量历史记录和媒体同步增加索引。

Revision ID: b7c8d9e0f1a2
Revises: 9a1b2c3d4e5f
"""

from alembic import op


revision = "b7c8d9e0f1a2"
down_revision = "9a1b2c3d4e5f"
branch_labels = None
depends_on = None


def _deduplicate(table, columns):
    joined = ", ".join(columns)
    predicate = "ENCLOSURE IS NOT NULL AND " if table == "RSS_TORRENTS" else ""
    op.execute(
        f"DELETE FROM {table} WHERE {predicate}ID NOT IN "
        f"(SELECT MIN(ID) FROM {table} GROUP BY {joined})"
    )


def upgrade():
    # 升级旧数据库时保留最早的记录，清理重复数据后再创建索引。
    for table, columns in (
            ("RSS_TORRENTS", ("ENCLOSURE",)),
            ("SYNC_HISTORY", ("PATH", "DEST")),
            ("SITE_BRUSH_TORRENTS", ("TASK_ID", "TORRENT_NAME", "ENCLOSURE"))):
        try:
            _deduplicate(table, columns)
        except Exception as exc:
            print(str(exc))

    statements = (
        "CREATE UNIQUE INDEX IF NOT EXISTS UN_INDX_RSS_TORRENTS_ENCLOSURE "
        "ON RSS_TORRENTS(ENCLOSURE)",
        "CREATE UNIQUE INDEX IF NOT EXISTS UN_INDX_SYNC_HISTORY_PATH_DEST "
        "ON SYNC_HISTORY(PATH, DEST)",
        "CREATE INDEX IF NOT EXISTS INDX_TRANSFER_HISTORY_SOURCE_DEST "
        "ON TRANSFER_HISTORY(SOURCE_PATH, SOURCE_FILENAME, DEST_PATH, DEST_FILENAME)",
        "CREATE UNIQUE INDEX IF NOT EXISTS UN_INDX_SITE_BRUSH_TORRENTS_TASK_NAME_ENCLOSURE "
        "ON SITE_BRUSH_TORRENTS(TASK_ID, TORRENT_NAME, ENCLOSURE)",
        "CREATE INDEX IF NOT EXISTS INDX_SITE_BRUSH_TORRENTS_TASK_DOWNLOAD "
        "ON SITE_BRUSH_TORRENTS(TASK_ID, DOWNLOAD_ID)",
    )
    for statement in statements:
        try:
            op.execute(statement)
        except Exception as exc:
            print(str(exc))
    try:
        op.execute("DROP INDEX IF EXISTS INDX_SITE_STATISTICS_HISTORY_DS")
    except Exception as exc:
        print(str(exc))


def downgrade():
    for name in (
            "UN_INDX_SYNC_HISTORY_PATH_DEST",
            "INDX_TRANSFER_HISTORY_SOURCE_DEST",
            "UN_INDX_SITE_BRUSH_TORRENTS_TASK_NAME_ENCLOSURE",
            "INDX_SITE_BRUSH_TORRENTS_TASK_DOWNLOAD"):
        try:
            op.execute(f"DROP INDEX IF EXISTS {name}")
        except Exception as exc:
            print(str(exc))
