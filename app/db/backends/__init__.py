"""业务数据库后端。"""

from app.db.backends.mariadb import MariaDBBackend
from app.db.backends.mysql import MySQLBackend
from app.db.backends.postgresql import PostgreSQLBackend
from app.db.backends.sqlite import SQLiteBackend


BACKENDS = {
    "sqlite": SQLiteBackend,
    "postgresql": PostgreSQLBackend,
    "mysql": MySQLBackend,
    "mariadb": MariaDBBackend,
}


def create_backend(name):
    backend = BACKENDS.get(str(name or "sqlite").lower())
    if backend is None:
        raise ValueError("数据库类型必须是 sqlite、postgresql、mysql 或 mariadb")
    return backend()
