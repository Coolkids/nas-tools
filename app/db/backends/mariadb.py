"""MariaDB 通过 PyMySQL 的 MariaDB 专用 SQLAlchemy dialect 连接。"""

from sqlalchemy import URL, create_engine

from app.db.backends.mysql import MySQLBackend


class MariaDBBackend(MySQLBackend):
    name = "mariadb"

    def create_engine(self, settings):
        url = URL.create(
            "mariadb+pymysql",
            username=settings.get("username") or None,
            password=settings.get("password") or None,
            host=settings.get("host"),
            port=int(settings.get("port") or 3306),
            database=settings.get("name"),
            query={"charset": "utf8mb4"},
        )
        connect_args = {"connect_timeout": int(settings.get("connect_timeout", 5))}
        ssl = settings.get("ssl")
        if isinstance(ssl, dict) and ssl:
            connect_args["ssl"] = ssl
        return create_engine(url, pool_pre_ping=True, pool_recycle=1800,
                             connect_args=connect_args)
