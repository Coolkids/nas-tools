"""Store a compact recognition list projection beside on-demand raw payloads."""

import json

import sqlalchemy as sa
from alembic import op


revision = "f1a2b3c4d5e6"
down_revision = "e0f1a2b3c4d5"
branch_labels = None
depends_on = None


def _decode(value, fallback):
    if value is None:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _identity(value):
    if not isinstance(value, dict):
        return None
    media_type = value.get("media_type")
    if isinstance(media_type, dict):
        media_type = media_type.get("value")
    return {"id": value.get("id"), "media_type": media_type,
            "title": value.get("title") or value.get("name")}


def _summary(row):
    overall = _decode(row["OVERALL_RESULT"], {})
    providers = _decode(row["PROVIDER_RESULTS"], [])
    tmdb_results = _decode(row["TMDB_RESULTS"], [])
    actions = _decode(row["ACTIONS"], [])
    overall = overall if isinstance(overall, dict) else {}
    providers = providers if isinstance(providers, list) else []
    tmdb_results = tmdb_results if isinstance(tmdb_results, list) else []
    actions = actions if isinstance(actions, list) else []
    return {
        "overall_result": {
            "status": overall.get("status"), "reason": overall.get("reason"),
            "selected_provider": overall.get("selected_provider"),
            "elapsed_ms": overall.get("elapsed_ms"),
            "tmdb_result": _identity(overall.get("tmdb_result")),
        },
        "provider_results": [{key: item.get(key) for key in (
            "attempt_id", "provider_id", "status", "normalized_result", "elapsed_ms", "error")}
            for item in providers if isinstance(item, dict)],
        "tmdb_results": [{"provider_id": item.get("provider_id"),
                           "status": item.get("status"),
                           "result": _identity(item.get("result"))}
                          for item in tmdb_results if isinstance(item, dict)],
        "actions": [{key: item.get(key) for key in ("sequence", "action_type", "status")}
                    for item in actions if isinstance(item, dict)],
    }


def upgrade():
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("RECOGNITION_REQUEST")}
    if "SUMMARY" not in columns:
        op.add_column("RECOGNITION_REQUEST", sa.Column("SUMMARY", sa.Text(), nullable=True))

    last_id = ""
    while True:
        rows = bind.execute(sa.text("""
            SELECT REQUEST_ID, SUMMARY, OVERALL_RESULT, PROVIDER_RESULTS, TMDB_RESULTS, ACTIONS
            FROM RECOGNITION_REQUEST
            WHERE SUMMARY IS NULL AND REQUEST_ID > :last_id
            ORDER BY REQUEST_ID LIMIT 500
        """), {"last_id": last_id}).mappings().all()
        if not rows:
            break
        for row in rows:
            bind.execute(sa.text("""
                UPDATE RECOGNITION_REQUEST SET SUMMARY = :summary WHERE REQUEST_ID = :request_id
            """), {"summary": json.dumps(_summary(row), ensure_ascii=False,
                                        separators=(",", ":")),
                   "request_id": row["REQUEST_ID"]})
        last_id = rows[-1]["REQUEST_ID"]


def downgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("RECOGNITION_REQUEST")}
    if "SUMMARY" in columns:
        op.drop_column("RECOGNITION_REQUEST", "SUMMARY")
