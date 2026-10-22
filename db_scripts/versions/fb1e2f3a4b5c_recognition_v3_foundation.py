"""Additive V3 foundations only. Legacy writers/tables remain until S20/S21."""

import sqlalchemy as sa
from alembic import op

revision = "fb1e2f3a4b5c"
down_revision = "fa0d1e2f3a4b"
branch_labels = None
depends_on = None

TABLES = ("RECOGNITION_TRACE", "RECOGNITION_EVENT", "RECOGNITION_CANDIDATE",
          "RECOGNITION_TASK", "RECOGNITION_OVERRIDE", "RECOGNITION_SECRET", "RECOGNITION_RECORD_STATE")
LEGACY_DROP_ALLOWLIST = ("RECOGNITION_REQUEST_ACTION", "RECOGNITION_ATTEMPT", "RECOGNITION_REQUEST", "AI_RECOGNITION_RECORD")


def upgrade():
    from app.db.models import Base
    bind = op.get_bind()
    names = {name.upper() for name in sa.inspect(bind).get_table_names()}
    if "MEDIASYNC_ITEMS" in names and "CONFIG_USERS" not in names:
        return
    for name in TABLES:
        Base.metadata.tables[name].create(bind, checkfirst=True)


def downgrade():
    raise RuntimeError("V3 recognition downgrade requires a verified database backup; no automatic data removal")
