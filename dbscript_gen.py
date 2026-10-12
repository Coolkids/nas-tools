import os
from config import Config
from app.db.manager import DATABASE
from alembic.config import Config as AlembicConfig
from alembic.command import revision as alembic_revision

db_version = input("请输入版本号：")
script_location = os.path.join(os.path.dirname(__file__), 'db_scripts').replace('\\', '/')
alembic_cfg = AlembicConfig()
alembic_cfg.set_main_option('script_location', script_location)
with DATABASE.engine.connect() as connection:
    alembic_cfg.attributes['connection'] = connection
    alembic_revision(alembic_cfg, message=db_version, autogenerate=True)
