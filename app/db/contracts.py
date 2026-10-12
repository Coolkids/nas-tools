"""数据库后端共用的轻量契约。"""

from dataclasses import dataclass


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
