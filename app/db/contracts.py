"""数据库后端共用的轻量契约。"""

from dataclasses import dataclass
import re

from sqlalchemy import LargeBinary, String, func, insert, inspect, select

from app.db.models import portable_key_digest, validate_string_lengths


@dataclass(frozen=True)
class DatabaseStatus:
    backend: str
    connected: bool
    url: str
    supports_space_reclaim: bool


class DatabaseBackend:
    """数据库引擎适配器；业务仓储只使用统一 SQLAlchemy 会话。"""

    name = ""
    supports_space_reclaim = False

    def create_engine(self, settings):
        raise NotImplementedError

    def configure_connection(self, connection):
        """按后端设置连接级参数。"""
        return None

    def adapt_seed_sql(self, sql, metadata):
        """将受控内置种子脚本转换为当前后端可执行的语句。"""
        return _adapt_seed_sql(sql, metadata, self.name)

    def capabilities(self, engine):
        with engine.connect() as connection:
            connection.execute(select(1)).scalar_one()
            version = engine.dialect.server_version_info
        return {"product": self.name, "version": str(version) if version else None}

    def validate_product(self, capabilities):
        expected = {"sqlite": "SQLite", "postgresql": "PostgreSQL",
                    "mysql": "MySQL", "mariadb": "MariaDB"}.get(self.name)
        if expected and capabilities.get("product") != expected:
            raise RuntimeError(
                f"配置选择 {expected}，但检测到 {capabilities.get('product') or '未知数据库产品'}")

    def inspect_schema(self, engine):
        inspector = inspect(engine)
        return {name: inspector.get_columns(name) for name in inspector.get_table_names()}

    def table_counts(self, engine, metadata):
        inspector = inspect(engine)
        available = {name.upper() for name in inspector.get_table_names()}
        missing = {name.upper() for name in metadata.tables} - available
        if missing:
            raise RuntimeError(f"数据库缺少应用表：{', '.join(sorted(missing))}")
        with engine.connect() as connection:
            return {table.name: int(connection.execute(
                select(func.count()).select_from(table)).scalar_one())
                    for table in metadata.sorted_tables}

    def estimated_table_counts(self, engine, metadata):
        reason = "数据库未提供该表的记录数估算；可异步刷新精确记录数"
        return ({table.name: None for table in metadata.sorted_tables},
                {table.name: reason for table in metadata.sorted_tables})

    def storage_usage(self, engine, metadata):
        return {"application_data_bytes": None, "application_index_bytes": None,
                "database_size_bytes": None,
                "measurement_reason": "当前数据库后端不提供空间统计"}

    def ordered_select(self, table):
        primary_keys = list(table.primary_key.columns)
        if not primary_keys:
            raise RuntimeError(f"表 {table.name} 缺少稳定主键，无法读取批次")
        order_by = []
        for column in primary_keys:
            if (isinstance(column.type, (String, LargeBinary))
                or getattr(column.type, "python_type", None) is str):
                if self.name == "postgresql":
                    order_by.append(func.convert_to(column.cast(String), "UTF8"))
                elif self.name in {"mysql", "mariadb"}:
                    order_by.append(func.binary(column))
                elif self.name == "sqlite":
                    order_by.append(column.cast(LargeBinary))
                else:
                    order_by.append(column)
            else:
                order_by.append(column)
        return select(*table.columns).order_by(*order_by)

    def read_batches(self, connection, table, batch_size=500):
        result = connection.execute(
            self.ordered_select(table).execution_options(stream_results=True))
        try:
            while True:
                rows = result.fetchmany(max(1, int(batch_size)))
                if not rows:
                    break
                yield [dict(row._mapping) for row in rows]
        finally:
            result.close()

    @staticmethod
    def insert_many(connection, table, rows):
        if rows:
            validate_string_lengths(table, rows)
            connection.execute(insert(table), rows)
        return len(rows)

    def calibrate_sequences(self, engine, metadata):
        """Backends without independent sequences need no explicit calibration."""
        return None

    def max_payload_bytes(self, engine):
        return None

    @staticmethod
    def release(engine):
        engine.dispose()

    def reclaim_space(self, engine):
        raise NotImplementedError(f"{self.name} 不支持空间回收")

    def checkpoint(self, engine, mode="PASSIVE"):
        """执行后端支持的低干扰日志维护；默认后端无需处理。"""
        return None

    def optimize(self, engine):
        """在结构升级后的低负载窗口刷新后端查询规划统计。"""
        return None

    @staticmethod
    def _portable_rows(session, model, rows, conflict_columns):
        """为跨数据库长唯一键生成确定性摘要，并在碰撞时拒绝覆盖。"""
        table = model.__table__
        conflict_columns = tuple(conflict_columns)
        rows = [dict(row) for row in rows]
        validate_string_lengths(table, rows)
        portable_keys = getattr(model, "__portable_unique_keys__", {})
        digest_column_name = portable_keys.get(conflict_columns)
        if not digest_column_name:
            return rows, conflict_columns

        digest_column = table.c[digest_column_name]
        values_by_digest = {}
        for row in rows:
            key_values = tuple(row.get(column) for column in conflict_columns)
            digest = portable_key_digest(key_values)
            row[digest_column_name] = digest
            if digest is not None:
                previous = values_by_digest.setdefault(digest, key_values)
                if previous != key_values:
                    raise ValueError("摘要唯一键冲突，拒绝覆盖不同的原始值")

        digest_values = list(values_by_digest)
        original_columns = [table.c[column] for column in conflict_columns]
        for offset in range(0, len(digest_values), 500):
            statement = select(digest_column, *original_columns).where(
                digest_column.in_(digest_values[offset:offset + 500]))
            for existing in session.execute(statement):
                digest = existing[0]
                if values_by_digest[digest] != tuple(existing[1:]):
                    raise ValueError("数据库中发现摘要唯一键冲突，拒绝覆盖不同记录")
        return rows, (digest_column_name,)

    def upsert_many(self, session, model, rows, conflict_columns, update_columns=None):
        """后端统一入口；具体冲突 SQL 由方言适配器实现。"""
        raise NotImplementedError(f"{self.name} 尚未实现批量 upsert")


def _quote_seed_identifiers(sql, backend):
    """Convert quoted identifiers outside SQL string literals for MySQL."""
    if backend not in {"mysql", "mariadb"}:
        return sql
    output = []
    index = 0
    in_string = False
    while index < len(sql):
        char = sql[index]
        if char == "'":
            output.append(char)
            if in_string and index + 1 < len(sql) and sql[index + 1] == "'":
                output.append("'")
                index += 2
                continue
            in_string = not in_string
            index += 1
            continue
        if char == '"' and not in_string:
            end = sql.find('"', index + 1)
            if end > index:
                identifier = sql[index + 1:end]
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
                    output.append("`" + identifier.replace("`", "``") + "`")
                    index = end + 1
                    continue
        output.append(char)
        index += 1
    return "".join(output)


def _quote_postgresql_update_identifiers(sql, metadata):
    """Quote known table and column names in legacy UPDATE seed statements."""
    match = re.match(
        r"(?is)^(?P<prefix>\s*UPDATE\s+)(?P<table>[A-Za-z_][A-Za-z0-9_]*)(?P<set>\s+SET\b)",
        sql,
    )
    if not match:
        return sql
    table = metadata.tables.get(match.group("table").upper())
    if table is None:
        return sql
    columns = {column.name.upper(): column.name for column in table.columns}
    output = [match.group("prefix"), f'"{table.name}"', match.group("set")]
    index = match.end()
    while index < len(sql):
        char = sql[index]
        if char in ("'", '"'):
            quote = char
            end = index + 1
            while end < len(sql):
                if sql[end] == quote:
                    if end + 1 < len(sql) and sql[end + 1] == quote:
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            output.append(sql[index:end])
            index = end
            continue
        if char.isalpha() or char == "_":
            end = index + 1
            while end < len(sql) and (sql[end].isalnum() or sql[end] == "_"):
                end += 1
            identifier = sql[index:end]
            column_name = columns.get(identifier.upper())
            output.append(f'"{column_name}"' if column_name else identifier)
            index = end
            continue
        output.append(char)
        index += 1
    return "".join(output)


def _adapt_seed_sql(sql, metadata, backend):
    statement = str(sql).strip().rstrip(";").strip()
    if not statement:
        return statement
    statement = re.sub(r"(?i)\bUPDATE\s+main\.", "UPDATE ", statement)
    statement = re.sub(r"(?i)(\bWHERE\s+)1\s*$", r"\1TRUE", statement)
    if backend == "postgresql":
        statement = _quote_postgresql_update_identifiers(statement, metadata)
    else:
        statement = _quote_seed_identifiers(statement, backend)
    match = re.match(
        r"(?is)^INSERT\s+OR\s+(IGNORE|REPLACE)\s+INTO\s+"
        r"(?P<table>\"[^\"]+\"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][A-Za-z0-9_]*)"
        r"\s*\((?P<columns>[^)]*)\)\s*VALUES\b",
        statement,
    )
    if not match:
        return statement
    operation = match.group(1).upper()
    table_name = match.group("table").strip('"`[]').upper()
    table = metadata.tables.get(table_name)
    if table is None:
        raise ValueError(f"初始化脚本引用了未受控的数据表：{table_name}")
    column_names = [item.strip().strip('"`[]') for item in match.group("columns").split(",")]
    primary_keys = [column.name for column in table.primary_key.columns]
    if not primary_keys:
        raise ValueError(f"初始化脚本目标表缺少主键：{table_name}")
    if operation == "REPLACE" and not set(primary_keys).issubset(column_names):
        raise ValueError(f"初始化替换语句未提供完整主键：{table_name}")
    normalized = "INSERT INTO " + statement[match.start("table"):]
    if backend in {"mysql", "mariadb"}:
        if operation == "IGNORE":
            key = primary_keys[0].replace("`", "``")
            suffix = f" ON DUPLICATE KEY UPDATE `{key}`=`{key}`"
        else:
            update_columns = [name for name in column_names if name not in primary_keys]
            if not update_columns:
                key = primary_keys[0].replace("`", "``")
                suffix = f" ON DUPLICATE KEY UPDATE `{key}`=`{key}`"
            else:
                suffix = " ON DUPLICATE KEY UPDATE " + ", ".join(
                    f"`{name.replace('`', '``')}`=VALUES(`{name.replace('`', '``')}`)"
                    for name in update_columns)
    elif operation == "IGNORE":
        suffix = " ON CONFLICT DO NOTHING"
    else:
        update_columns = [name for name in column_names if name not in primary_keys]
        if not update_columns:
            suffix = " ON CONFLICT DO NOTHING"
        else:
            keys = ", ".join(f'"{name}"' for name in primary_keys)
            updates = ", ".join(
                f'"{name}"=excluded."{name}"' for name in update_columns)
            suffix = f" ON CONFLICT ({keys}) DO UPDATE SET {updates}"
    return normalized + suffix
