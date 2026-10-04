import importlib.util
import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import sessionmaker

from app.db.models import RECOGNITIONATTEMPT, RECOGNITIONREQUEST
from app.helper.db_helper import DbHelper
from web.action import WebAction


MIGRATION_PATH = Path(__file__).resolve().parents[1] / "db_scripts" / "versions" / \
    "f1a2b3c4d5e6_add_recognition_summaries.py"
SPEC = importlib.util.spec_from_file_location("recognition_summary_migration", MIGRATION_PATH)
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


class RecognitionPayloadLoadingTest(unittest.TestCase):
    def test_expired_recognition_cleanup_deletes_in_batches_and_keeps_running_rows(self):
        engine = sa.create_engine("sqlite:///:memory:")
        RECOGNITIONREQUEST.__table__.create(engine)
        RECOGNITIONATTEMPT.__table__.create(engine)
        session = sessionmaker(bind=engine)()

        class TestDb:
            @staticmethod
            def query(*models):
                return session.query(*models)

        previous_db = DbHelper._db
        DbHelper._db = TestDb()
        try:
            session.add_all([
                RECOGNITIONREQUEST(REQUEST_ID="old-1", CREATED_AT="2026-08-01 10:00:00",
                                   OVERALL_RESULT='{"status":"success"}'),
                RECOGNITIONREQUEST(REQUEST_ID="old-2", CREATED_AT="2026-08-02 10:00:00",
                                   OVERALL_RESULT='{"status":"failed"}'),
                RECOGNITIONREQUEST(REQUEST_ID="running", CREATED_AT="2026-08-01 10:00:00",
                                   OVERALL_RESULT='{"status":"running"}'),
                RECOGNITIONREQUEST(REQUEST_ID="recent", CREATED_AT="2026-09-20 10:00:00",
                                   OVERALL_RESULT='{"status":"success"}'),
            ])
            session.add_all([
                RECOGNITIONATTEMPT(REQUEST_ID=request_id, ATTEMPT_ID=f"{request_id}-attempt")
                for request_id in ("old-1", "old-2", "running", "recent")
            ])
            session.commit()

            with patch.object(previous_db, "commit"):
                deleted = DbHelper().delete_expired_recognition_records(
                    30, now=datetime.datetime(2026, 10, 4), batch_size=1)

            self.assertEqual(2, deleted)
            self.assertEqual({"running", "recent"}, {
                row.REQUEST_ID for row in session.query(RECOGNITIONREQUEST).all()})
            self.assertEqual({"running", "recent"}, {
                row.REQUEST_ID for row in session.query(RECOGNITIONATTEMPT).all()})
        finally:
            DbHelper._db = previous_db
            session.close()
            engine.dispose()

    def test_list_query_leaves_full_payload_columns_unloaded(self):
        engine = sa.create_engine("sqlite:///:memory:")
        RECOGNITIONREQUEST.__table__.create(engine)
        RECOGNITIONATTEMPT.__table__.create(engine)
        session = sessionmaker(bind=engine)()

        class TestDb:
            @staticmethod
            def query(*models):
                return session.query(*models)

        previous_db = DbHelper._db
        DbHelper._db = TestDb()
        try:
            session.add(RECOGNITIONREQUEST(
                REQUEST_ID="r1", ORIGINAL_NAME="Example", SOURCE="test", STAGE="resolve",
                CREATED_AT="2026-10-02 10:00:00", SUMMARY='{"overall_result":{"status":"success"}}',
                CONTEXT='{"large":"context"}', ACTIONS='[{"large":"actions"}]',
                PROVIDER_RESULTS='[{"raw_result":"large"}]',
                OVERALL_RESULT='{"status":"success"}', TMDB_RESULTS='[{"large":"tmdb"}]'))
            session.commit()

            _, rows = DbHelper().get_recognition_records(page_size=10)

            self.assertEqual("r1", rows[0].REQUEST_ID)
            self.assertEqual('{"overall_result":{"status":"success"}}', rows[0].SUMMARY)
            self.assertTrue({"CONTEXT", "ACTIONS", "PROVIDER_RESULTS", "OVERALL_RESULT", "TMDB_RESULTS"}
                            .issubset(sa.inspect(rows[0]).unloaded))
            helper = Mock()
            helper.get_recognition_records.return_value = (1, rows)
            with patch("web.action.DbHelper", return_value=helper):
                response = WebAction._WebAction__get_recognition_records({})
            self.assertEqual(0, response["code"])
            self.assertEqual("success", response["records"][0]["overall_result"]["status"])
            self.assertTrue({"CONTEXT", "ACTIONS", "PROVIDER_RESULTS", "OVERALL_RESULT", "TMDB_RESULTS"}
                            .issubset(sa.inspect(rows[0]).unloaded))
        finally:
            DbHelper._db = previous_db
            session.close()
            engine.dispose()

    def test_migration_backfills_compact_summaries_and_is_repeatable(self):
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as connection:
            connection.exec_driver_sql("""
                CREATE TABLE RECOGNITION_REQUEST (
                    REQUEST_ID TEXT PRIMARY KEY, ORIGINAL_NAME TEXT, SOURCE TEXT,
                    STAGE TEXT, CREATED_AT TEXT, CONTEXT TEXT, ACTIONS TEXT,
                    PROVIDER_RESULTS TEXT, OVERALL_RESULT TEXT, TMDB_RESULTS TEXT
                )
            """)
            connection.exec_driver_sql("""
                INSERT INTO RECOGNITION_REQUEST VALUES (
                    'r1', 'Example', 'test', 'resolve', '2026-10-02', '{}',
                    '[{"sequence":1,"action_type":"provider_parse","status":"success","input":{"large":"omitted"}}]',
                    '[{"attempt_id":"a1","provider_id":"local_rules","status":"success","raw_result":{"large":"omitted"},"normalized_result":{"name":"Example"}}]',
                    '{"status":"success","selected_provider":"local_rules","tmdb_result":{"id":7,"media_type":"tv","name":"Example"}}',
                    '[{"provider_id":"local_rules","status":"success","result":{"id":7,"media_type":"tv","name":"Example","overview":"large"}}]'
                )
            """)
            with Operations.context(MigrationContext.configure(connection)):
                MIGRATION.upgrade()
                MIGRATION.upgrade()
            value = connection.exec_driver_sql(
                "SELECT SUMMARY FROM RECOGNITION_REQUEST WHERE REQUEST_ID='r1'").scalar_one()

        summary = json.loads(value)
        self.assertEqual("success", summary["overall_result"]["status"])
        self.assertEqual({"id": 7, "media_type": "tv", "title": "Example"},
                         summary["overall_result"]["tmdb_result"])
        self.assertEqual("Example", summary["provider_results"][0]["normalized_result"]["name"])
        self.assertNotIn("raw_result", summary["provider_results"][0])
        self.assertNotIn("overview", summary["tmdb_results"][0]["result"])

    def test_archive_preserves_by_default_and_deletes_attempts_only_on_explicit_request(self):
        engine = sa.create_engine("sqlite:///:memory:")
        RECOGNITIONREQUEST.__table__.create(engine)
        RECOGNITIONATTEMPT.__table__.create(engine)
        session = sessionmaker(bind=engine)()

        class TestDb:
            @staticmethod
            def query(*models):
                return session.query(*models)

            @staticmethod
            def commit():
                session.commit()

            @staticmethod
            def rollback():
                session.rollback()

        previous_db = DbHelper._db
        DbHelper._db = TestDb()
        try:
            session.add(RECOGNITIONREQUEST(
                REQUEST_ID="old", ORIGINAL_NAME="Old title", SOURCE="test", STAGE="resolve",
                CREATED_AT="2020-01-01 00:00:00", SUMMARY="{}", CONTEXT='{"raw":true}',
                ACTIONS="[]", PROVIDER_RESULTS='[{"raw_result":"kept"}]',
                OVERALL_RESULT='{"status":"success"}', TMDB_RESULTS="[]"))
            session.add(RECOGNITIONATTEMPT(
                REQUEST_ID="old", ATTEMPT_ID="a1", PROVIDER_ID="local_rules", STATUS="success",
                INPUT="{}", RAW_RESULT='{"raw":true}', NORMALIZED_RESULT='{"name":"Old"}',
                TMDB_RESULTS="[]", ERROR=None, ELAPSED_MS=1))
            session.commit()

            with tempfile.TemporaryDirectory() as directory:
                archive = Path(directory) / "recognition.jsonl"
                preserved = DbHelper().archive_recognition_records(
                    "2021-01-01", str(archive))
                self.assertEqual(1, preserved["archived_count"])
                self.assertEqual(0, preserved["deleted_count"])
                self.assertEqual(1, session.query(RECOGNITIONREQUEST).count())
                archived = json.loads(archive.read_text(encoding="utf-8"))
                self.assertEqual('{"raw":true}', archived["attempts"][0]["raw_result"])
                with self.assertRaises(FileExistsError):
                    DbHelper().archive_recognition_records("2021-01-01", str(archive))

                deleted_archive = Path(directory) / "recognition-deleted.jsonl"
                deleted = DbHelper().archive_recognition_records(
                    "2021-01-01", str(deleted_archive), delete_archived=True)
                self.assertEqual(1, deleted["deleted_count"])
                self.assertEqual(0, session.query(RECOGNITIONREQUEST).count())
                self.assertEqual(0, session.query(RECOGNITIONATTEMPT).count())
        finally:
            DbHelper._db = previous_db
            session.close()
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
