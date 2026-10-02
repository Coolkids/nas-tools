# -*- coding: utf-8 -*-
import datetime
import json
import os
import tempfile
import unittest

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.db.models import RECOGNITIONATTEMPT, RECOGNITIONREQUEST
from app.helper.db_helper import DbHelper
from web.action import WebAction


class RecognitionExportPaginationTest(unittest.TestCase):
    def test_jsonl_export_returns_every_row_across_many_pages(self):
        engine = sa.create_engine("sqlite:///:memory:")
        RECOGNITIONREQUEST.__table__.create(engine)
        RECOGNITIONATTEMPT.__table__.create(engine)
        session = sessionmaker(bind=engine)()
        reader_session = session

        class TestDb:
            session = reader_session

            @staticmethod
            def query(*models):
                return session.query(*models)

        previous_db = DbHelper._db
        DbHelper._db = TestDb()
        try:
            session.bulk_insert_mappings(RECOGNITIONREQUEST, [{
                "REQUEST_ID": f"request-{index:04d}",
                "ORIGINAL_NAME": f"Example {index}",
                "SOURCE": "test",
                "STAGE": "resolve",
                "CREATED_AT": "2020-01-01 00:00:00.000000",
                "CONTEXT": "{}",
                "ACTIONS": "[]",
                "PROVIDER_RESULTS": "[]",
                "OVERALL_RESULT": '{"status": "success"}',
                "TMDB_RESULTS": "[]",
            } for index in range(1000)])
            session.commit()

            records = [json.loads(line) for line in WebAction.iter_recognition_jsonl(page_size=73)]

            request_ids = [record["request_id"] for record in records]
            self.assertEqual(1000, len(records))
            self.assertEqual(1000, len(set(request_ids)))
            self.assertEqual("request-0999", request_ids[0])
            self.assertEqual("request-0000", request_ids[-1])
        finally:
            DbHelper._db = previous_db
            session.close()
            engine.dispose()

    def test_jsonl_export_keeps_rows_deleted_after_snapshot_started(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "snapshot.db")
            engine = sa.create_engine(f"sqlite:///{database_path}", connect_args={"timeout": 5})
            RECOGNITIONREQUEST.__table__.create(engine)
            RECOGNITIONATTEMPT.__table__.create(engine)
            with engine.begin() as connection:
                connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            sessions = sessionmaker(bind=engine, expire_on_commit=False)
            writer = sessions()
            reader = sessions()

            class TestDb:
                session = reader

                @staticmethod
                def query(*models):
                    return reader.query(*models)

            previous_db = DbHelper._db
            DbHelper._db = TestDb()
            try:
                writer.add_all([
                    RECOGNITIONREQUEST(
                        REQUEST_ID=f"r{index}", ORIGINAL_NAME=f"Example {index}",
                        SOURCE="test", STAGE="resolve",
                        CREATED_AT=f"2026-10-02 10:00:0{index}.000000",
                        CONTEXT="{}", ACTIONS="[]", PROVIDER_RESULTS="[]",
                        OVERALL_RESULT='{"status": "success"}', TMDB_RESULTS="[]")
                    for index in range(1, 4)
                ])
                writer.commit()

                exported = WebAction.iter_recognition_jsonl(page_size=1)
                first = json.loads(next(exported))
                self.assertEqual("r3", first["request_id"])

                writer.query(RECOGNITIONREQUEST).filter(
                    RECOGNITIONREQUEST.REQUEST_ID.in_(["r1", "r2"])).delete(
                        synchronize_session=False)
                writer.commit()

                remaining = [json.loads(line)["request_id"] for line in exported]
                self.assertEqual(["r2", "r1"], remaining)
                self.assertFalse(reader.in_transaction())
            finally:
                DbHelper._db = previous_db
                reader.close()
                writer.close()
                engine.dispose()

    def test_combined_filters_respect_inclusive_date_bounds_and_stable_order(self):
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
                RECOGNITIONREQUEST(
                    REQUEST_ID="match-b", ORIGINAL_NAME="Example Movie 2024", SOURCE="rss",
                    STAGE="resolve", CREATED_AT="2026-10-02 23:59:59.999999",
                    CONTEXT="{}", ACTIONS='[{"action_type": "tmdb_query"}]',
                    PROVIDER_RESULTS="[]", OVERALL_RESULT=(
                        '{"status": "failed", "reason": "no_tmdb_match"}'), TMDB_RESULTS="[]"),
                RECOGNITIONREQUEST(
                    REQUEST_ID="match-a", ORIGINAL_NAME="Example Movie 2024", SOURCE="rss",
                    STAGE="resolve", CREATED_AT="2026-10-02 00:00:00.000000",
                    CONTEXT="{}", ACTIONS='[{"action_type": "tmdb_query"}]',
                    PROVIDER_RESULTS="[]", OVERALL_RESULT=(
                        '{"status": "failed", "reason": "no_tmdb_match"}'), TMDB_RESULTS="[]"),
                RECOGNITIONREQUEST(
                    REQUEST_ID="wrong-source", ORIGINAL_NAME="Example Movie 2024", SOURCE="web",
                    STAGE="resolve", CREATED_AT="2026-10-02 12:00:00.000000",
                    CONTEXT="{}", ACTIONS='[{"action_type": "tmdb_query"}]',
                    PROVIDER_RESULTS="[]", OVERALL_RESULT=(
                        '{"status": "failed", "reason": "no_tmdb_match"}'), TMDB_RESULTS="[]"),
                RECOGNITIONREQUEST(
                    REQUEST_ID="after-range", ORIGINAL_NAME="Example Movie 2024", SOURCE="rss",
                    STAGE="resolve", CREATED_AT="2026-10-03 00:00:00.000000",
                    CONTEXT="{}", ACTIONS='[{"action_type": "tmdb_query"}]',
                    PROVIDER_RESULTS="[]", OVERALL_RESULT=(
                        '{"status": "failed", "reason": "no_tmdb_match"}'), TMDB_RESULTS="[]"),
            ])
            session.add_all([
                RECOGNITIONATTEMPT(
                    REQUEST_ID=request_id, ATTEMPT_ID=f"attempt-{request_id}",
                    PROVIDER_ID="fixture_third", STATUS="success", INPUT="{}",
                    RAW_RESULT="{}", NORMALIZED_RESULT="{}", TMDB_RESULTS="[]")
                for request_id in ("match-a", "match-b", "wrong-source", "after-range")
            ])
            session.commit()

            total, records = DbHelper().get_recognition_records(
                title="Example", source="rss", status="failed", provider_id="fixture_third",
                action_type="tmdb_query", reason="no_tmdb_match",
                created_from="2026-10-02 00:00:00.000000",
                created_to="2026-10-02 23:59:59.999999", page_size=10)

            self.assertEqual(2, total)
            self.assertEqual(["match-b", "match-a"], [row.REQUEST_ID for row in records])
        finally:
            DbHelper._db = previous_db
            session.close()
            engine.dispose()

    def test_keyset_pages_keep_snapshot_when_new_records_arrive(self):
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
                RECOGNITIONREQUEST(REQUEST_ID=f"r{i}", ORIGINAL_NAME=f"title {i}",
                                   SOURCE="test", STAGE="resolve",
                                   CREATED_AT=f"2026-10-02 10:00:0{i}.000000",
                                   CONTEXT="{}", ACTIONS="[]", PROVIDER_RESULTS="[]",
                                   OVERALL_RESULT='{"status":"success"}', TMDB_RESULTS="[]")
                for i in range(1, 5)
            ])
            session.commit()

            helper = DbHelper()
            first_page = helper.get_recognition_records(
                page_size=2, snapshot_at="2026-10-02 10:00:05.000000",
                include_total=False)[1]
            cursor = (first_page[-1].CREATED_AT, first_page[-1].REQUEST_ID)

            session.add(RECOGNITIONREQUEST(
                REQUEST_ID="late", ORIGINAL_NAME="new title", SOURCE="test",
                STAGE="resolve", CREATED_AT="2026-10-02 10:00:06.000000",
                CONTEXT="{}", ACTIONS="[]", PROVIDER_RESULTS="[]",
                OVERALL_RESULT='{"status":"success"}', TMDB_RESULTS="[]"))
            session.commit()

            second_page = helper.get_recognition_records(
                page_size=2, snapshot_at="2026-10-02 10:00:05.000000",
                before_cursor=cursor, include_total=False)[1]
            self.assertEqual(["r4", "r3"], [row.REQUEST_ID for row in first_page])
            self.assertEqual(["r2", "r1"], [row.REQUEST_ID for row in second_page])
            self.assertTrue(set(row.REQUEST_ID for row in first_page).isdisjoint(
                row.REQUEST_ID for row in second_page))
        finally:
            DbHelper._db = previous_db
            session.close()
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
