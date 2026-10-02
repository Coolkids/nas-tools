# -*- coding: utf-8 -*-
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


MIGRATION_PATH = Path(__file__).resolve().parents[1] / "db_scripts" / "versions" / \
    "e0f1a2b3c4d5_migrate_legacy_recognition.py"
SPEC = importlib.util.spec_from_file_location("legacy_recognition_migration", MIGRATION_PATH)
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


class LegacyRecognitionMigrationTest(TestCase):
    def test_migration_preserves_raw_values_and_does_not_infer_a_decision(self):
        local_raw = {
            "normalized": {"name": "Example", "year": "2020"},
            "parsed": {"anime_title": "Example"},
        }
        ai_raw = {"title": "Example", "confidence": 0.92}
        row = {
            "ID": 17,
            "TITLE": "Example.2020.1080p",
            "ANITOPY_RESULT": json.dumps(local_raw),
            "AI_RESULT": json.dumps(ai_raw),
            "ANITOPY_TMDB": json.dumps({"id": 10, "media_type": "movie"}),
            "AI_TMDB": json.dumps({"id": 20, "media_type": "movie"}),
            "STATUS": "inconsistent",
            "ADD_TIME": "2025-01-02 03:04:05",
        }

        request, attempts = MIGRATION.map_legacy_row(row)
        providers = json.loads(request["PROVIDER_RESULTS"])
        overall = json.loads(request["OVERALL_RESULT"])
        tmdb = json.loads(request["TMDB_RESULTS"])

        self.assertEqual("Example.2020.1080p", request["ORIGINAL_NAME"])
        self.assertEqual("legacy-ai-recognition-17", request["REQUEST_ID"])
        self.assertEqual("Example", providers[0]["normalized_result"]["name"])
        self.assertEqual(local_raw, providers[0]["raw_result"])
        self.assertEqual("unknown", overall["status"])
        self.assertEqual("inconsistent", overall["legacy_status"])
        self.assertIsNone(overall["selected_provider"])
        self.assertEqual([], json.loads(request["ACTIONS"]))
        self.assertEqual([10, 20], [item["result"]["id"] for item in tmdb])
        self.assertEqual(2, len(attempts))

    def test_null_legacy_fields_are_not_misrepresented_as_failures(self):
        request, attempts = MIGRATION.map_legacy_row({
            "ID": 3, "TITLE": "unresolved", "ANITOPY_RESULT": None,
            "AI_RESULT": None, "ANITOPY_TMDB": None, "AI_TMDB": None,
            "STATUS": "unmatched", "ADD_TIME": None,
        })

        self.assertEqual([], attempts)
        self.assertEqual([], json.loads(request["PROVIDER_RESULTS"]))
        self.assertTrue(all(item["status"] == "not_recorded"
                            for item in json.loads(request["TMDB_RESULTS"])))
        self.assertEqual("unmatched", json.loads(request["OVERALL_RESULT"])["legacy_status"])

    def test_upgrade_copies_rows_in_a_repeatable_way(self):
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as connection:
            connection.exec_driver_sql("""
                CREATE TABLE AI_RECOGNITION_RECORD (
                    ID INTEGER PRIMARY KEY, TITLE TEXT, ANITOPY_RESULT TEXT,
                    AI_RESULT TEXT, ANITOPY_TMDB TEXT, AI_TMDB TEXT,
                    STATUS TEXT, ADD_TIME TEXT
                )
            """)
            connection.exec_driver_sql("""
                CREATE TABLE RECOGNITION_REQUEST (
                    REQUEST_ID TEXT PRIMARY KEY, ORIGINAL_NAME TEXT, SOURCE TEXT,
                    STAGE TEXT, CREATED_AT TEXT, CONTEXT TEXT, ACTIONS TEXT,
                    PROVIDER_RESULTS TEXT, OVERALL_RESULT TEXT, TMDB_RESULTS TEXT
                )
            """)
            connection.exec_driver_sql("""
                CREATE TABLE RECOGNITION_ATTEMPT (
                    ID INTEGER PRIMARY KEY, REQUEST_ID TEXT, ATTEMPT_ID TEXT UNIQUE,
                    PROVIDER_ID TEXT, STATUS TEXT, INPUT TEXT, RAW_RESULT TEXT,
                    NORMALIZED_RESULT TEXT, TMDB_RESULTS TEXT, ERROR TEXT, ELAPSED_MS INTEGER
                )
            """)
            connection.exec_driver_sql("""
                INSERT INTO AI_RECOGNITION_RECORD VALUES
                (1, 'One', '{"normalized":{"name":"One"}}', '{"title":"One"}',
                 '{"id":11}', '{"id":12}', 'inconsistent', '2025-01-01'),
                (2, 'Two', NULL, NULL, NULL, NULL, 'unmatched', '2025-01-02')
            """)

            with Operations.context(MigrationContext.configure(connection)):
                MIGRATION.upgrade()
                MIGRATION.upgrade()

            requests = connection.exec_driver_sql(
                "SELECT REQUEST_ID, OVERALL_RESULT FROM RECOGNITION_REQUEST ORDER BY REQUEST_ID"
            ).fetchall()
            attempts = connection.exec_driver_sql(
                "SELECT REQUEST_ID, PROVIDER_ID FROM RECOGNITION_ATTEMPT ORDER BY REQUEST_ID, PROVIDER_ID"
            ).fetchall()

        self.assertEqual(2, len(requests))
        self.assertEqual(2, len(attempts))
        self.assertTrue(all(json.loads(row[1])["status"] == "unknown" for row in requests))
        self.assertEqual({"local_rules", "anitopy_ml"}, {row[1] for row in attempts})
