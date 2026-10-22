"""Retire old recognition histories after verified pre-upgrade native restore."""

from alembic import op, context

revision = "fc2f3a4b5c6d"
down_revision = "fb1e2f3a4b5c"
branch_labels = None
depends_on = None


def upgrade():
    from app.db.recognition_upgrade import finalize_migration
    attributes = context.get_context().config.attributes
    finalize_migration(op.get_bind(), attributes.get("recognition_upgrade_verified"),
                       attributes.get("recognition_upgrade_checkpoint", lambda _stage: None))


def downgrade():
    raise RuntimeError("回退旧二进制必须恢复升级前已校验的原生数据库备份及主密钥；禁止结构降级代替数据恢复")
