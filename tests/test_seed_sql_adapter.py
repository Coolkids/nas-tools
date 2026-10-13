import unittest
from unittest.mock import patch

from app.db.backends.postgresql import PostgreSQLBackend
from app.db.main_db import MainDb
from app.db.models import Base


class SeedSqlAdapterTest(unittest.TestCase):
    def test_postgresql_quotes_legacy_update_table_and_column_names(self):
        backend = PostgreSQLBackend()

        adapted = backend.adapt_seed_sql(
            "UPDATE CONFIG_USER_RSS SET PROCESS_COUNT = '0' "
            "WHERE PROCESS_COUNT is null",
            Base.metadata,
        )

        self.assertEqual(
            'UPDATE "CONFIG_USER_RSS" SET "PROCESS_COUNT" = \'0\' '
            'WHERE "PROCESS_COUNT" is null',
            adapted,
        )

    def test_postgresql_removes_legacy_schema_and_preserves_string_literals(self):
        backend = PostgreSQLBackend()

        adapted = backend.adapt_seed_sql(
            "UPDATE main.CONFIG_USERS SET PRIS = replace(PRIS, '推荐', '探索') WHERE 1",
            Base.metadata,
        )

        self.assertEqual(
            'UPDATE "CONFIG_USERS" SET "PRIS" = replace("PRIS", \'推荐\', \'探索\') WHERE TRUE',
            adapted,
        )

    def test_seed_startup_calibrates_postgresql_sequences(self):
        with patch("app.db.main_db.DATABASE") as database:
            backend = database.backend
            backend.name = "postgresql"

            MainDb._calibrate_seed_sequences()

        database.calibrate_sequences.assert_called_once_with(Base.metadata)

    def test_seed_startup_leaves_other_backend_sequence_management_unchanged(self):
        with patch("app.db.main_db.DATABASE") as database:
            backend = database.backend
            backend.name = "mysql"

            MainDb._calibrate_seed_sequences()

        database.calibrate_sequences.assert_not_called()


if __name__ == "__main__":
    unittest.main()
