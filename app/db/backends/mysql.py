"""MySQL 后端，连接字符集固定为 utf8mb4。"""

from sqlalchemy import URL, create_engine

from app.db.contracts import DatabaseBackend


class MySQLBackend(DatabaseBackend):
    name = "mysql"

    def create_engine(self, settings):
        url = URL.create(
            "mysql+pymysql",
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
