import unittest
import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


MIGRATION_PATH = Path(__file__).resolve().parents[1] / "db_scripts" / "versions" / \
    "f5a6b7c8d9e0_move_legacy_startup_cleanup.py"
SPEC = importlib.util.spec_from_file_location("legacy_site_cleanup_migration", MIGRATION_PATH)
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


class SiteAccountStatisticsRemovalTest(unittest.TestCase):
    def test_migration_drops_legacy_site_account_data_and_credentials(self):
        engine = create_engine("sqlite:///:memory:")
        legacy_tables = (
            "SITE_STATISTICS_HISTORY", "SITE_USER_INFO_STATS",
            "SITE_USER_SEEDING_INFO", "SITE_FAVICON"
        )
        with engine.begin() as conn:
            for table in legacy_tables:
                conn.execute(text(f"CREATE TABLE {table} (ID INTEGER PRIMARY KEY)"))
            conn.execute(text(
                "CREATE TABLE SYSTEM_DICT (TYPE TEXT, KEY TEXT, VALUE TEXT)"))
            conn.execute(text(
                "INSERT INTO SYSTEM_DICT (TYPE, KEY, VALUE) VALUES "
                "('SystemConfig', 'CookieUserInfo', '{\"username\":\"old\"}')"))

        try:
            with engine.begin() as conn:
                with Operations.context(MigrationContext.configure(conn)):
                    MIGRATION.upgrade()

            table_names = set(inspect(engine).get_table_names())
            self.assertTrue(set(legacy_tables).isdisjoint(table_names))
            with engine.connect() as conn:
                remaining_credentials = conn.execute(text(
                    "SELECT COUNT(*) FROM SYSTEM_DICT WHERE TYPE = 'SystemConfig' "
                    "AND KEY = 'CookieUserInfo'")).scalar_one()
            self.assertEqual(0, remaining_credentials)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
