"""保存本地解析与 AI 解析的 TMDB 匹配差异。"""

from alembic import op


revision = "c8d9e0f1a2b3"
down_revision = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS AI_RECOGNITION_RECORD (
            ID INTEGER PRIMARY KEY,
            TITLE TEXT,
            ANITOPY_RESULT TEXT,
            AI_RESULT TEXT,
            ANITOPY_TMDB TEXT,
            AI_TMDB TEXT,
            STATUS TEXT,
            ADD_TIME TEXT
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_recognition_record_title ON AI_RECOGNITION_RECORD(TITLE)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_recognition_record_status ON AI_RECOGNITION_RECORD(STATUS)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_recognition_record_add_time ON AI_RECOGNITION_RECORD(ADD_TIME)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_ai_recognition_record_add_time")
    op.execute("DROP INDEX IF EXISTS ix_ai_recognition_record_status")
    op.execute("DROP INDEX IF EXISTS ix_ai_recognition_record_title")
    op.execute("DROP TABLE IF EXISTS AI_RECOGNITION_RECORD")
