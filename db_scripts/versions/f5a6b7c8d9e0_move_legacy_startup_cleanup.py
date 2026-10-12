"""Move historical database cleanup out of the recurring startup path."""

import sqlalchemy as sa
from alembic import op


revision = "f5a6b7c8d9e0"
down_revision = "f4a5b6c7d8e9"
branch_labels = None
depends_on = None


_RETIRED_TABLES = (
    "SITE_STATISTICS_HISTORY", "SITE_USER_INFO_STATS", "SITE_USER_SEEDING_INFO",
    "SITE_FAVICON", "CONFIG_SITE",
)


def _table_lookup(bind):
    return {name.casefold(): name for name in sa.inspect(bind).get_table_names()}


def upgrade():
    bind = op.get_bind()
    tables = _table_lookup(bind)

    # The site-management feature was removed. Drop only known legacy tables.
    for legacy_name in _RETIRED_TABLES:
        actual_name = tables.get(legacy_name.casefold())
        if actual_name:
            sa.Table(actual_name, sa.MetaData(), autoload_with=bind).drop(bind)

    # Subscriptions no longer store private site selections.
    tables = _table_lookup(bind)
    for legacy_name in ("RSS_MOVIES", "RSS_TVS"):
        actual_name = tables.get(legacy_name.casefold())
        if not actual_name:
            continue
        reflected = sa.Table(actual_name, sa.MetaData(), autoload_with=bind)
        rss_sites = next((column for column in reflected.columns
                          if column.name.casefold() == "rss_sites"), None)
        if rss_sites is not None:
            bind.execute(sa.update(reflected).values({rss_sites: None}))

    # Remove credentials left by the retired site auto-login feature.
    actual_name = tables.get("system_dict")
    if actual_name:
        reflected = sa.Table(actual_name, sa.MetaData(), autoload_with=bind)
        columns = {column.name.casefold(): column for column in reflected.columns}
        if "type" in columns and "key" in columns:
            bind.execute(sa.delete(reflected).where(
                columns["type"] == "SystemConfig",
                columns["key"].in_(("CookieUserInfo", "CookieCloud"))))


def downgrade():
    raise RuntimeError("历史站点表及清理数据不可逆，拒绝自动降级")
