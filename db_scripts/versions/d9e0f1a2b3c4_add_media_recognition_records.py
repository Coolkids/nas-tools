"""Store every media-name recognition request and provider attempt."""

from alembic import op


revision = "d9e0f1a2b3c4"
down_revision = "c8d9e0f1a2b3"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS RECOGNITION_REQUEST (
            REQUEST_ID TEXT PRIMARY KEY,
            ORIGINAL_NAME TEXT,
            SOURCE TEXT,
            STAGE TEXT,
            CREATED_AT TEXT,
            CONTEXT TEXT,
            ACTIONS TEXT,
            PROVIDER_RESULTS TEXT,
            OVERALL_RESULT TEXT,
            TMDB_RESULTS TEXT
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_recognition_request_original_name ON RECOGNITION_REQUEST(ORIGINAL_NAME)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_recognition_request_source ON RECOGNITION_REQUEST(SOURCE)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_recognition_request_created_at ON RECOGNITION_REQUEST(CREATED_AT)")
    op.execute("""
        CREATE TABLE IF NOT EXISTS RECOGNITION_ATTEMPT (
            ID INTEGER PRIMARY KEY,
            REQUEST_ID TEXT,
            ATTEMPT_ID TEXT,
            PROVIDER_ID TEXT,
            STATUS TEXT,
            INPUT TEXT,
            RAW_RESULT TEXT,
            NORMALIZED_RESULT TEXT,
            TMDB_RESULTS TEXT,
            ERROR TEXT,
            ELAPSED_MS INTEGER
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_recognition_attempt_request_id ON RECOGNITION_ATTEMPT(REQUEST_ID)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_recognition_attempt_provider_id ON RECOGNITION_ATTEMPT(PROVIDER_ID)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_recognition_attempt_provider_id")
    op.execute("DROP INDEX IF EXISTS ix_recognition_attempt_request_id")
    op.execute("DROP TABLE IF EXISTS RECOGNITION_ATTEMPT")
    op.execute("DROP INDEX IF EXISTS ix_recognition_request_created_at")
    op.execute("DROP INDEX IF EXISTS ix_recognition_request_source")
    op.execute("DROP INDEX IF EXISTS ix_recognition_request_original_name")
    op.execute("DROP TABLE IF EXISTS RECOGNITION_REQUEST")
