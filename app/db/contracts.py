"""数据库后端共用的轻量契约。"""

from dataclasses import dataclass
import re

from sqlalchemy import select

from app.db.models import portable_key_digest


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

    def reclaim_space(self, engine):
        raise NotImplementedError(f"{self.name} 不支持空间回收")

    @staticmethod
    def _portable_rows(session, model, rows, conflict_columns):
        """为跨数据库长唯一键生成确定性摘要，并在碰撞时拒绝覆盖。"""
        table = model.__table__
        conflict_columns = tuple(conflict_columns)
        portable_keys = getattr(model, "__portable_unique_keys__", {})
        digest_column_name = portable_keys.get(conflict_columns)
        if not digest_column_name:
            return rows, conflict_columns

        digest_column = table.c[digest_column_name]
        values_by_digest = {}
        rows = [dict(row) for row in rows]
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


def _adapt_seed_sql(sql, metadata, backend):
    statement = str(sql).strip().rstrip(";").strip()
    if not statement:
        return statement
    statement = re.sub(r"(?i)\bUPDATE\s+main\.", "UPDATE ", statement)
    statement = re.sub(r"(?i)(\bWHERE\s+)1\s*$", r"\1TRUE", statement)
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
