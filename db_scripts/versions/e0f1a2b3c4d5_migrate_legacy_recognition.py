"""Copy historical dual-recognizer rows into the extensible recognition log."""

import json

import sqlalchemy as sa
from alembic import op


revision = "e0f1a2b3c4d5"
down_revision = "d9e0f1a2b3c4"
branch_labels = None
depends_on = None


def _decode(value):
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def map_legacy_row(row):
    """Preserve historical values without inferring missing actions or choices."""
    request_id = f"legacy-ai-recognition-{row['ID']}"
    original_name = row.get("TITLE")
    local_raw = _decode(row.get("ANITOPY_RESULT"))
    ai_raw = _decode(row.get("AI_RESULT"))
    local_tmdb = _decode(row.get("ANITOPY_TMDB"))
    ai_tmdb = _decode(row.get("AI_TMDB"))

    provider_results = []
    attempts = []
    tmdb_results = []
    for provider_id, raw_result, tmdb_result in (
            ("local_rules", local_raw, local_tmdb),
            ("anitopy_ml", ai_raw, ai_tmdb)):
        attempt_id = f"{request_id}:{provider_id}"
        normalized = raw_result
        if provider_id == "local_rules" and isinstance(raw_result, dict) \
                and "normalized" in raw_result:
            normalized = raw_result.get("normalized")
        if raw_result is not None:
            provider_results.append({
                "attempt_id": attempt_id,
                "provider_id": provider_id,
                "version": None,
                "status": "unknown",
                "input": {"title": original_name},
                "raw_result": raw_result,
                "normalized_result": normalized,
                "elapsed_ms": None,
                "error": None,
            })
        tmdb_entry = {
            "provider_id": provider_id,
            "query": None,
            "status": "unknown" if tmdb_result is not None else "not_recorded",
            "result": tmdb_result,
            "reason": None,
        }
        tmdb_results.append(tmdb_entry)
        if raw_result is not None:
            attempts.append({
                "REQUEST_ID": request_id,
                "ATTEMPT_ID": attempt_id,
                "PROVIDER_ID": provider_id,
                "STATUS": "unknown",
                "INPUT": _encode({"title": original_name}),
                "RAW_RESULT": _encode(raw_result),
                "NORMALIZED_RESULT": _encode(normalized),
                "TMDB_RESULTS": _encode([tmdb_entry]),
                "ERROR": None,
                "ELAPSED_MS": None,
            })

    overall_result = {
        "status": "unknown",
        "reason": "legacy_decision_not_recorded",
        "legacy_status": row.get("STATUS"),
        "parsed_result": None,
        "selected_provider": None,
        "tmdb_result": None,
    }
    request = {
        "REQUEST_ID": request_id,
        "ORIGINAL_NAME": original_name,
        "SOURCE": "legacy_ai_recognition",
        "STAGE": "resolve",
        "CREATED_AT": row.get("ADD_TIME"),
        "CONTEXT": _encode({}),
        "ACTIONS": _encode([]),
        "PROVIDER_RESULTS": _encode(provider_results),
        "OVERALL_RESULT": _encode(overall_result),
        "TMDB_RESULTS": _encode(tmdb_results),
    }
    return request, attempts


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "AI_RECOGNITION_RECORD" not in inspector.get_table_names():
        return

    legacy_rows = bind.execute(sa.text(
        "SELECT ID, TITLE, ANITOPY_RESULT, AI_RESULT, ANITOPY_TMDB, AI_TMDB, STATUS, ADD_TIME "
        "FROM AI_RECOGNITION_RECORD ORDER BY ID"
    ))
    request_insert = sa.text("""
        INSERT OR IGNORE INTO RECOGNITION_REQUEST
        (REQUEST_ID, ORIGINAL_NAME, SOURCE, STAGE, CREATED_AT, CONTEXT, ACTIONS,
         PROVIDER_RESULTS, OVERALL_RESULT, TMDB_RESULTS)
        VALUES (:REQUEST_ID, :ORIGINAL_NAME, :SOURCE, :STAGE, :CREATED_AT, :CONTEXT,
                :ACTIONS, :PROVIDER_RESULTS, :OVERALL_RESULT, :TMDB_RESULTS)
    """)
    attempt_insert = sa.text("""
        INSERT OR IGNORE INTO RECOGNITION_ATTEMPT
        (REQUEST_ID, ATTEMPT_ID, PROVIDER_ID, STATUS, INPUT, RAW_RESULT,
         NORMALIZED_RESULT, TMDB_RESULTS, ERROR, ELAPSED_MS)
        VALUES (:REQUEST_ID, :ATTEMPT_ID, :PROVIDER_ID, :STATUS, :INPUT, :RAW_RESULT,
                :NORMALIZED_RESULT, :TMDB_RESULTS, :ERROR, :ELAPSED_MS)
    """)
    while True:
        rows = legacy_rows.mappings().fetchmany(500)
        if not rows:
            break
        for row in rows:
            request, attempts = map_legacy_row(row)
            bind.execute(request_insert, request)
            if attempts:
                bind.execute(attempt_insert, attempts)


def downgrade():
    bind = op.get_bind()
    bind.execute(sa.text(
        "DELETE FROM RECOGNITION_ATTEMPT WHERE REQUEST_ID LIKE 'legacy-ai-recognition-%'"
    ))
    bind.execute(sa.text(
        "DELETE FROM RECOGNITION_REQUEST WHERE REQUEST_ID LIKE 'legacy-ai-recognition-%'"
    ))
