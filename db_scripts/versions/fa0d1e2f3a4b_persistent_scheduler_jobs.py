"""Persist independent APScheduler interval schedules in the application database."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import LONGBLOB


revision = "fa0d1e2f3a4b"
down_revision = "f9c0d1e2f3a4"
branch_labels = None
depends_on = None

TABLES = ("SCHEDULER_JOBS", "RSS_SCHEDULER_JOBS", "TORRENT_REMOVER_JOBS", "SPEED_LIMITER_JOBS")


def upgrade():
    bind = op.get_bind()
    names = {name.upper() for name in sa.inspect(bind).get_table_names()}
    # 旧双库备份会单独升级媒体快照，不能把主库调度表创建到媒体来源库。
    if "MEDIASYNC_ITEMS" in names and "CONFIG_USERS" not in names:
        return
    for name in TABLES:
        table = sa.Table(
            name, sa.MetaData(),
            sa.Column("id", sa.String(191), primary_key=True),
            sa.Column("next_run_time", sa.Float(precision=53), index=True),
            sa.Column("job_state", sa.LargeBinary().with_variant(LONGBLOB(), "mysql", "mariadb"),
                      nullable=False),
        )
        table.create(bind, checkfirst=True)


def downgrade():
    names = set(sa.inspect(op.get_bind()).get_table_names())
    for name in reversed(TABLES):
        if name in names:
            op.drop_table(name)
