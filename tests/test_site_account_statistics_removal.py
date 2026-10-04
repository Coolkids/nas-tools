import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, text

import app.db.main_db as main_db


class SiteAccountStatisticsRemovalTest(unittest.TestCase):
    def test_startup_drops_legacy_site_account_data_and_credentials(self):
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
            with patch.object(main_db, "_Engine", engine), \
                    patch.object(main_db.MainDb, "wal_checkpoint"):
                main_db.MainDb.init_db()

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
