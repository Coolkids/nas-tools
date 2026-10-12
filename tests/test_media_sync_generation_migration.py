"""媒体同步代次结构升级兼容旧索引和统计数据。"""

import importlib.util
from pathlib import Path
from unittest import TestCase

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


MIGRATION_PATH = Path(__file__).resolve().parents[1] / "db_scripts" / "versions" / \
    "f2a3b4c5d6e7_media_sync_generations.py"
SPEC = importlib.util.spec_from_file_location("media_sync_generation_migration", MIGRATION_PATH)
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)
LEASE_MIGRATION_PATH = Path(__file__).resolve().parents[1] / "db_scripts" / "versions" / \
    "f3a4b5c6d7e8_media_sync_job_leases.py"
LEASE_SPEC = importlib.util.spec_from_file_location("media_sync_job_lease_migration",
                                                   LEASE_MIGRATION_PATH)
LEASE_MIGRATION = importlib.util.module_from_spec(LEASE_SPEC)
LEASE_SPEC.loader.exec_module(LEASE_MIGRATION)


class MediaSyncGenerationMigrationTest(TestCase):
    def test_upgrade_backfills_generation_and_creates_unique_server_statistics(self):
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as connection:
            connection.exec_driver_sql("""CREATE TABLE MEDIASYNC_ITEMS (
                ID INTEGER PRIMARY KEY, SERVER VARCHAR(128), LIBRARY TEXT, ITEM_ID VARCHAR(256),
                ITEM_TYPE TEXT, TITLE TEXT, ORGIN_TITLE TEXT, YEAR TEXT, TMDBID TEXT,
                IMDBID TEXT, PATH TEXT, NOTE TEXT, JSON TEXT)""")
            connection.exec_driver_sql("""CREATE UNIQUE INDEX UN_INDX_MEDIASYNC_ITEMS_SERVER_ITEM
                ON MEDIASYNC_ITEMS(SERVER, ITEM_ID)""")
            connection.exec_driver_sql("""CREATE TABLE MEDIASYNC_STATISTICS (
                ID INTEGER PRIMARY KEY, SERVER TEXT, TOTAL_COUNT TEXT, MOVIE_COUNT TEXT,
                TV_COUNT TEXT, UPDATE_TIME TEXT)""")
            connection.exec_driver_sql("CREATE INDEX ix_MEDIASYNC_STATISTICS_SERVER "
                                       "ON MEDIASYNC_STATISTICS(SERVER)")
            connection.exec_driver_sql("""INSERT INTO MEDIASYNC_ITEMS
                (ID, SERVER, ITEM_ID, ITEM_TYPE, TITLE) VALUES
                (1, 'emby', 'movie-1', 'Movie', 'Movie'),
                (2, 'emby', 'series-1', 'Series', 'Series'),
                (3, 'plex', 'movie-2', 'movie', 'Other movie')""")
            connection.exec_driver_sql("""INSERT INTO MEDIASYNC_STATISTICS
                (ID, SERVER, TOTAL_COUNT, MOVIE_COUNT, TV_COUNT, UPDATE_TIME) VALUES
                (1, 'emby', '1', '1', '0', 'old'),
                (2, 'emby', '2', '1', '1', 'new')""")

            with Operations.context(MigrationContext.configure(connection)):
                MIGRATION.upgrade()
                MIGRATION.upgrade()

            items = connection.exec_driver_sql("""SELECT SERVER, GENERATION_ID, ITEM_ID
                FROM MEDIASYNC_ITEMS ORDER BY ID""").fetchall()
            statistics = connection.exec_driver_sql("""SELECT SERVER, ACTIVE_GENERATION,
                TOTAL_COUNT, MOVIE_COUNT, TV_COUNT, UPDATE_TIME
                FROM MEDIASYNC_STATISTICS ORDER BY SERVER""").fetchall()
            indexes = {item["name"]: item for item in sa.inspect(connection).get_indexes(
                "MEDIASYNC_ITEMS")}
            stats_indexes = {item["name"]: item for item in sa.inspect(connection).get_indexes(
                "MEDIASYNC_STATISTICS")}

        engine.dispose()
        self.assertEqual([("emby", "legacy", "movie-1"),
                          ("emby", "legacy", "series-1"),
                          ("plex", "legacy", "movie-2")], items)
        self.assertEqual(2, len(statistics))
        self.assertEqual(("emby", "legacy", "2", "1", "1", "new"), statistics[0])
        self.assertEqual(("plex", "legacy", "1", "1", "0"), statistics[1][:5])
        self.assertIn("UN_INDX_MEDIASYNC_ITEMS_SERVER_GENERATION_ITEM", indexes)
        self.assertTrue(indexes["UN_INDX_MEDIASYNC_ITEMS_SERVER_GENERATION_ITEM"]["unique"])
        self.assertNotIn("UN_INDX_MEDIASYNC_ITEMS_SERVER_ITEM", indexes)
        self.assertNotIn("ix_MEDIASYNC_STATISTICS_SERVER", stats_indexes)
        self.assertTrue(stats_indexes["UN_INDX_MEDIASYNC_STATISTICS_SERVER"]["unique"])

    def test_job_lease_migration_is_repeatable_and_enforces_unique_job_ids(self):
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                LEASE_MIGRATION.upgrade()
                LEASE_MIGRATION.upgrade()
            inspector = sa.inspect(connection)
            self.assertIn("MEDIASYNC_SYNC_JOBS", inspector.get_table_names())
            columns = {column["name"] for column in inspector.get_columns("MEDIASYNC_SYNC_JOBS")}
            indexes = {index["name"]: index for index in inspector.get_indexes(
                "MEDIASYNC_SYNC_JOBS")}
            self.assertEqual({"SERVER", "JOB_ID", "GENERATION_ID", "LEASE_TOKEN",
                              "LEASE_UNTIL", "EXPECTED_ACTIVE_GENERATION", "STATUS",
                              "CREATED_AT", "UPDATED_AT"}, columns)
            self.assertTrue(indexes["UN_INDX_MEDIASYNC_SYNC_JOBS_JOB_ID"]["unique"])
        engine.dispose()
