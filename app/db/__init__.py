import os
import log
from config import Config
from app.db.manager import DATABASE
from .main_db import MainDb
from .main_db import DbPersist
from .main_db import get_db_metrics
from .media_db import MediaDb
from alembic.config import Config as AlembicConfig
from alembic.command import upgrade as alembic_upgrade


def close_db():
    """
    释放当前线程的数据库会话，归还连接到连接池
    应在请求结束时调用，避免连接泄漏导致 QueuePool 耗尽
    """
    try:
        DATABASE.close_session()
    except Exception as e:
        log.error(f"关闭数据库会话失败: {e}")


def init_db():
    """
    初始化数据库
    """
    log.console('开始初始化数据库...')
    MainDb().init_db()
    log.console('数据库初始化完成')


def init_data():
    """
    初始化数据
    """
    log.console('开始初始化数据...')
    MainDb().init_data()
    log.console('数据初始化完成')


def update_db():
    """升级活动数据库；旧 SQLite revision 只在 SQLite 后端执行。"""
    script_location = os.path.join(Config().get_root_path(), 'db_scripts')
    log.console('开始更新数据库...')
    engine = DATABASE.engine
    alembic_cfg = AlembicConfig()
    alembic_cfg.set_main_option('script_location', script_location)
    if DATABASE.backend.name == "sqlite":
        from alembic import command
        from alembic.script import ScriptDirectory
        from sqlalchemy import inspect, text
        from app.db.models import Base

        head = ScriptDirectory.from_config(alembic_cfg).get_current_head()
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        application_tables = tables - {"alembic_version", "__nastools_database_layout"}
        if not application_tables and "alembic_version" not in tables:
            # 新安装没有历史结构可迁移：先创建统一 metadata，再写入对应版本。
            Base.metadata.create_all(engine)
            with engine.begin() as connection:
                connection.execute(text(
                    "CREATE TABLE IF NOT EXISTS alembic_version "
                    "(version_num VARCHAR(32) NOT NULL)"))
                connection.execute(text("DELETE FROM alembic_version"))
                connection.execute(text(
                    "INSERT INTO alembic_version(version_num) VALUES (:version)"),
                    {"version": head})
        elif not application_tables:
            raise RuntimeError("SQLite 只有结构版本记录但缺少业务表，拒绝跳过升级")
        elif "alembic_version" not in tables:
            raise RuntimeError("现有 SQLite 数据库缺少结构版本，拒绝跳过升级")
        else:
            with engine.connect() as connection:
                alembic_cfg.attributes["connection"] = connection
                alembic_upgrade(alembic_cfg, 'head')
    else:
        # 历史 revision 含 SQLite 专用 SQL。新的远程空库直接按统一 metadata
        # 建立完整基线；已有远程结构必须由后续版本化迁移明确升级。
        from alembic.script import ScriptDirectory
        from sqlalchemy import inspect, text
        from app.db.models import Base

        head = ScriptDirectory.from_config(alembic_cfg).get_current_head()
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        if "alembic_version" in tables:
            with engine.connect() as connection:
                versions = [row[0] for row in connection.execute(
                    text("SELECT version_num FROM alembic_version"))]
            if versions != [head]:
                raise RuntimeError("远程数据库结构版本不受支持，需先执行受控升级")
        elif tables:
            raise RuntimeError("远程数据库不是空库，拒绝覆盖或直接标记结构版本")
        else:
            Base.metadata.create_all(engine)
            with engine.begin() as connection:
                connection.execute(text(
                    "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
                connection.execute(text("INSERT INTO alembic_version(version_num) VALUES (:version)"),
                                   {"version": head})
    from app.db.models import Base
    available_tables = {name.upper() for name in inspect(engine).get_table_names()}
    required_tables = {name.upper() for name in Base.metadata.tables}
    missing_tables = required_tables - available_tables
    if missing_tables:
        raise RuntimeError(f"数据库缺少应用表，结构升级未完成：{', '.join(sorted(missing_tables))}")
    log.console('数据库更新完成')
