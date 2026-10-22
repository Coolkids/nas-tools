"""Retired recognition names for historical snapshot conversion and S21 upgrade."""

TABLES = ("RECOGNITION_REQUEST_ACTION", "RECOGNITION_ATTEMPT", "RECOGNITION_REQUEST", "AI_RECOGNITION_RECORD")


def archive_metadata(include_v3=False):
    """Frozen columns of the two supported pre-retirement portable schemas.

    Migration-only metadata, never registered with the runtime ORM. Indexes and
    defaults were not part of portable archive schema declarations.
    """
    import sqlalchemy as sa
    from app.db.models import Base, LargeText, configure_portable_indexes
    from app.db.recognition_upgrade import V3_TABLES
    metadata = sa.MetaData()
    for table in Base.metadata.sorted_tables:
        if include_v3 or table.name not in V3_TABLES:
            table.to_metadata(metadata)
    sa.Table("AI_RECOGNITION_RECORD", metadata,
             sa.Column("ID", sa.Integer, primary_key=True),
             sa.Column("TITLE", sa.Text),
             *[sa.Column(name, LargeText) for name in ("ANITOPY_RESULT", "AI_RESULT", "ANITOPY_TMDB", "AI_TMDB")],
             sa.Column("STATUS", sa.Text), sa.Column("ADD_TIME", sa.Text))
    sa.Table("RECOGNITION_REQUEST", metadata,
             sa.Column("REQUEST_ID", sa.String(128), primary_key=True),
             *[sa.Column(name, sa.Text) for name in ("ORIGINAL_NAME", "SOURCE", "STAGE", "CREATED_AT", "SUMMARY")],
             sa.Column("STATUS_SUMMARY", sa.String(64)), sa.Column("BUSINESS_STATUS_SUMMARY", sa.String(64)),
             sa.Column("REASON_SUMMARY", sa.Text), sa.Column("REASON_HASH", sa.String(64)),
             sa.Column("BUSINESS_REASON_SUMMARY", sa.Text), sa.Column("BUSINESS_REASON_HASH", sa.String(64)),
             *[sa.Column(name, LargeText) for name in ("CONTEXT", "ACTIONS", "PROVIDER_RESULTS", "OVERALL_RESULT", "TMDB_RESULTS")])
    sa.Table("RECOGNITION_ATTEMPT", metadata,
             sa.Column("ID", sa.Integer, primary_key=True),
             *[sa.Column(name, sa.String(128)) for name in ("REQUEST_ID", "ATTEMPT_ID", "PROVIDER_ID")],
             sa.Column("STATUS", sa.Text),
             *[sa.Column(name, LargeText) for name in ("INPUT", "RAW_RESULT", "NORMALIZED_RESULT", "TMDB_RESULTS", "ERROR")],
             sa.Column("ELAPSED_MS", sa.Integer))
    sa.Table("RECOGNITION_REQUEST_ACTION", metadata,
             sa.Column("ID", sa.String(64), primary_key=True),
             sa.Column("REQUEST_ID", sa.String(128), nullable=False),
             sa.Column("ACTION_TYPE", sa.String(256), nullable=False),
             sa.Column("ACTION_HASH", sa.String(64), nullable=False))
    configure_portable_indexes(metadata)
    return metadata
