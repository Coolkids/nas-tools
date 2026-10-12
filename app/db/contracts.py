"""数据库后端共用的轻量契约。"""

from dataclasses import dataclass

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
