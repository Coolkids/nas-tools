"""MySQL 后端，连接字符集固定为 utf8mb4。"""

from sqlalchemy import URL, create_engine, select, text, func
from sqlalchemy.dialects.mysql import insert as mysql_insert

from app.db.contracts import DatabaseBackend
from app.db.pool import MeasuredQueuePool


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
        return create_engine(url, poolclass=MeasuredQueuePool,
                             pool_pre_ping=True, pool_recycle=1800,
                             connect_args=connect_args)

    def capabilities(self, engine):
        with engine.connect() as connection:
            version = str(connection.execute(text("SELECT VERSION()")).scalar_one())
            charset = str(connection.execute(
                text("SELECT @@character_set_database")).scalar_one())
            collation = str(connection.execute(
                text("SELECT @@collation_database")).scalar_one())
            sql_mode = str(connection.execute(text("SELECT @@sql_mode")).scalar_one() or "")
            packet = int(connection.execute(
                text("SELECT @@max_allowed_packet")).scalar_one())
            storage_engine = str(connection.execute(
                text("SELECT @@default_storage_engine")).scalar_one())
            lower_case_table_names = int(connection.execute(
                text("SELECT @@lower_case_table_names")).scalar_one())
            server_host, server_port, server_id, database = connection.execute(text(
                "SELECT @@hostname, @@port, @@server_id, DATABASE()"
            )).one()
        if charset.lower() != "utf8mb4":
            raise RuntimeError("MySQL/MariaDB 目标数据库默认字符集必须为 utf8mb4")
        if storage_engine.lower() not in {"innodb", "xtradb"}:
            raise RuntimeError("MySQL/MariaDB 默认存储引擎必须支持事务")
        return {"product": "MariaDB" if "mariadb" in version.lower() else "MySQL",
                "version": version, "character_set": charset, "collation": collation,
                "sql_mode": sql_mode, "max_allowed_packet": packet,
                "default_storage_engine": storage_engine,
                "lower_case_table_names": lower_case_table_names,
                "server_host": str(server_host), "server_port": int(server_port),
                "server_id": int(server_id), "database": database}

    def estimated_table_counts(self, engine, metadata):
        estimates = {}
        reasons = {}
        with engine.connect() as connection:
            for table in metadata.sorted_tables:
                row = connection.execute(text("""
                    SELECT TABLE_ROWS FROM information_schema.TABLES
                    WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table_name
                """), {"table_name": table.name}).first()
                if row is not None and row[0] is not None and int(row[0]) >= 0:
                    estimates[table.name] = int(row[0])
                else:
                    reasons[table.name] = "MySQL/MariaDB 未提供该表的记录数估算；可异步刷新精确记录数"
        return ({table.name: estimates.get(table.name) for table in metadata.sorted_tables}, reasons)

    def storage_usage(self, engine, metadata):
        try:
            data_bytes = index_bytes = 0
            with engine.connect() as connection:
                database_bytes = int(connection.execute(text("""
                    SELECT COALESCE(SUM(DATA_LENGTH + INDEX_LENGTH), 0)
                    FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()
                """)).scalar_one())
                for table in metadata.sorted_tables:
                    row = connection.execute(text("""
                        SELECT DATA_LENGTH, INDEX_LENGTH FROM information_schema.TABLES
                        WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table_name
                    """), {"table_name": table.name}).one_or_none()
                    if row is None:
                        raise RuntimeError(f"缺少表 {table.name} 的空间统计")
                    data_bytes += int(row[0] or 0)
                    index_bytes += int(row[1] or 0)
            return {"application_data_bytes": data_bytes,
                    "application_index_bytes": index_bytes,
                    "database_size_bytes": database_bytes, "measurement_reason": None}
        except Exception as error:
            return {"application_data_bytes": None, "application_index_bytes": None,
                    "database_size_bytes": None,
                    "measurement_reason": f"MySQL/MariaDB 空间统计不可用：{str(error)[:500]}"}

    def calibrate_sequences(self, engine, metadata):
        preparer = engine.dialect.identifier_preparer
        with engine.begin() as connection:
            for table in metadata.sorted_tables:
                column = getattr(table, "autoincrement_column", None)
                if column is None or getattr(column.type, "python_type", None) is not int:
                    continue
                maximum = connection.execute(
                    select(func.max(column)).select_from(table)).scalar_one()
                next_value = max(int(maximum or 0), 0) + 1
                connection.exec_driver_sql(
                    f"ALTER TABLE {preparer.quote(table.name)} AUTO_INCREMENT = {next_value}")

    @staticmethod
    def max_payload_bytes(engine):
        with engine.connect() as connection:
            return int(connection.execute(
                text("SELECT @@max_allowed_packet")).scalar_one())

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
