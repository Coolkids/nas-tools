"""PostgreSQL 同步驱动后端。"""

from sqlalchemy import URL, create_engine, select, text, func
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

    def capabilities(self, engine):
        with engine.connect() as connection:
            version = connection.execute(text("SHOW server_version")).scalar_one()
            version_num = connection.execute(text("SHOW server_version_num")).scalar_one()
            database = connection.execute(text("SELECT current_database()")).scalar_one()
            schema = connection.execute(text("SELECT current_schema()")).scalar_one()
        return {"product": "PostgreSQL", "version": str(version),
                "version_num": str(version_num), "database": database, "schema": schema}

    def estimated_table_counts(self, engine, metadata):
        estimates = {}
        reasons = {}
        with engine.connect() as connection:
            for table in metadata.sorted_tables:
                row = connection.execute(text("""
                    SELECT reltuples::bigint FROM pg_class
                    WHERE relname=:table_name AND relkind IN ('r', 'p')
                    ORDER BY (relnamespace = current_schema()::regnamespace) DESC LIMIT 1
                """), {"table_name": table.name}).first()
                if row is not None and int(row[0]) >= 0:
                    estimates[table.name] = int(row[0])
                else:
                    reasons[table.name] = "PostgreSQL 尚无该表的规划器估算；可异步刷新精确记录数"
        return ({table.name: estimates.get(table.name) for table in metadata.sorted_tables}, reasons)

    def storage_usage(self, engine, metadata):
        try:
            data_bytes = index_bytes = 0
            with engine.connect() as connection:
                database_bytes = int(connection.execute(
                    text("SELECT pg_database_size(current_database())")).scalar_one())
                for table in metadata.sorted_tables:
                    table_name = f'"{table.name}"'
                    data_size, index_size = connection.execute(text("""
                        SELECT pg_total_relation_size(CAST(:table_name AS regclass))
                               - pg_indexes_size(CAST(:table_name AS regclass)),
                               pg_indexes_size(CAST(:table_name AS regclass))
                    """), {"table_name": table_name}).one()
                    data_bytes += int(data_size or 0)
                    index_bytes += int(index_size or 0)
            return {"application_data_bytes": data_bytes,
                    "application_index_bytes": index_bytes,
                    "database_size_bytes": database_bytes, "measurement_reason": None}
        except Exception as error:
            return {"application_data_bytes": None, "application_index_bytes": None,
                    "database_size_bytes": None,
                    "measurement_reason": f"PostgreSQL 空间统计不可用：{str(error)[:500]}"}

    def calibrate_sequences(self, engine, metadata):
        with engine.begin() as connection:
            for table in metadata.sorted_tables:
                for column in table.primary_key.columns:
                    if (getattr(column.type, "python_type", None) is not int
                            or getattr(table, "autoincrement_column", None) is not column):
                        continue
                    sequence_name = connection.execute(text(
                        "SELECT pg_get_serial_sequence(:table_name, :column_name)"), {
                            "table_name": f'"{table.name}"',
                            "column_name": f'"{column.name}"',
                        }).scalar_one_or_none()
                    if not sequence_name:
                        continue
                    maximum = connection.execute(
                        select(func.max(column)).select_from(table)).scalar_one()
                    if maximum is None:
                        connection.execute(text(
                            "SELECT setval(CAST(:sequence_name AS regclass), 1, false)"),
                            {"sequence_name": sequence_name})
                    else:
                        connection.execute(text(
                            "SELECT setval(CAST(:sequence_name AS regclass), :value, true)"),
                            {"sequence_name": sequence_name, "value": int(maximum)})

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
