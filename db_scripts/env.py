from logging.config import fileConfig

from alembic import context

from app.db.models import Base
# 这是 Alembic Config 对象，可用于访问当前使用的 ini 文件中的配置。
config = context.config

# 读取配置文件中的 Python 日志设置。
# 此行会初始化日志记录器。
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 在此添加模型的 MetaData 对象，以支持自动生成迁移。
# 示例：from myapp import mymodel
# target_metadata = mymodel.Base.metadata
target_metadata = Base.metadata

# 也可以按 env.py 的需要读取配置中的其他值：
# my_important_option = config.get_main_option("my_important_option")
# 等等。


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """使用应用数据库管理器持有的活动连接执行升级。

    在此处另建引擎会绕过共享连接池、连接初始化钩子和维护闸门，
    也可能升级到与运行中应用不同的数据库。
    """
    def run_with_connection(connection):
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()

    supplied_connection = config.attributes.get("connection")
    if supplied_connection is None:
        raise RuntimeError(
            "在线数据库升级必须使用 DatabaseManager 提供的活动连接"
        )
    run_with_connection(supplied_connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
