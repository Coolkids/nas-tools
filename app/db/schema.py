"""统一的业务 metadata、逻辑域和结构版本入口。"""

from app.db.models import Base


MEDIA_TABLES = frozenset({"MEDIASYNC_ITEMS", "MEDIASYNC_STATISTICS", "MEDIASYNC_SYNC_JOBS"})
LOGICAL_DOMAIN_TABLES = {
    "main": frozenset(set(Base.metadata.tables) - MEDIA_TABLES),
    "media": MEDIA_TABLES,
}
SCHEMA_LAYOUT_VERSION = 1


def current_schema_revision(script_location=None):
    """读取 Alembic 当前唯一 head，不在导入模块时创建数据库连接。"""
    import os
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    if script_location is None:
        script_location = os.path.abspath(os.path.join(
            os.path.dirname(__file__), "..", "..", "db_scripts"))
    config = Config()
    config.set_main_option("script_location", script_location)
    return ScriptDirectory.from_config(config).get_current_head()
