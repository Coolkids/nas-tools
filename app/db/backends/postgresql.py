"""PostgreSQL 同步驱动后端。"""

from sqlalchemy import URL, create_engine
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from app.db.contracts import DatabaseBackend


class PostgreSQLBackend(DatabaseBackend):
    name = "postgresql"

    def create_engine(self, settings):
        ssl = settings.get("ssl") or {}
        query = {}
        if isinstance(ssl, dict) and ssl.get("mode"):
            query["sslmode"] = str(ssl["mode"])
        url = URL.create(
            "postgresql+psycopg",
            username=settings.get("username") or None,
            password=settings.get("password") or None,
            host=settings.get("host"),
            port=int(settings.get("port") or 5432),
            database=settings.get("name"),
            query=query,
        )
        return create_engine(url, pool_pre_ping=True, pool_recycle=1800,
                             connect_args={"connect_timeout": int(settings.get("connect_timeout", 5))})

    def upsert_many(self, session, model, rows, conflict_columns, update_columns=None):
        if not rows:
            return 0
        rows, conflict_columns = self._portable_rows(
            session, model, rows, conflict_columns)
        statement = postgresql_insert(model.__table__).values(rows)
        if update_columns:
            statement = statement.on_conflict_do_update(
                index_elements=list(conflict_columns),
                set_={column: getattr(statement.excluded, column)
                      for column in update_columns})
        else:
            statement = statement.on_conflict_do_nothing(
                index_elements=list(conflict_columns))
        session.execute(statement)
        return len(rows)
