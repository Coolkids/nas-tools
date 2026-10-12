"""MySQL 后端，连接字符集固定为 utf8mb4。"""

from sqlalchemy import URL, create_engine
from sqlalchemy.dialects.mysql import insert as mysql_insert

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

    def upsert_many(self, session, model, rows, conflict_columns, update_columns=None):
        if not rows:
            return 0
        table = model.__table__
        rows, conflict_columns = self._portable_rows(
            session, model, rows, conflict_columns)
        # MySQL/MariaDB route every unique-key conflict through ON DUPLICATE KEY.
        # Refuse ambiguous tables instead of treating a different unique-key
        # violation as a successful update.
        expected = {tuple(conflict_columns)}
        unique_sets = {tuple(column.name for column in constraint.columns)
                       for constraint in table.constraints
                       if getattr(constraint, "unique", False)}
        unique_sets.update(tuple(index.columns.keys()) for index in table.indexes
                           if index.unique)
        if unique_sets - expected:
            raise ValueError(f"{table.name} 存在无法安全区分的其他唯一键")

        statement = mysql_insert(table).values(rows)
        if update_columns:
            statement = statement.on_duplicate_key_update(
                **{column: getattr(statement.inserted, column)
                   for column in update_columns})
        else:
            primary_key = next(iter(table.primary_key.columns)).name
            statement = statement.on_duplicate_key_update(
                **{primary_key: table.c[primary_key]})
        session.execute(statement)
        return len(rows)
