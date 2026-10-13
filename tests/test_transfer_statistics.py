import datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.dialects.mysql.mariadb import MariaDBDialect
from sqlalchemy.orm import sessionmaker

from app.db.models import TRANSFERHISTORY
from app.helper.db_helper import DbHelper


def test_transfer_statistics_groups_by_type_and_date_in_order():
    engine = sa.create_engine("sqlite:///:memory:")
    TRANSFERHISTORY.__table__.create(engine)
    session = sessionmaker(bind=engine)()

    class TestDb:
        @staticmethod
        def query(*models):
            return session.query(*models)

    previous_db = DbHelper._db
    DbHelper._db = TestDb()
    try:
        today = datetime.datetime.now().date()
        day_one = (today - datetime.timedelta(days=2)).strftime("%Y-%m-%d")
        day_two = (today - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        session.add_all([
            TRANSFERHISTORY(TYPE="电影", DATE=f"{day_one} 09:00:00"),
            TRANSFERHISTORY(TYPE="电影", DATE=f"{day_one} 10:00:00"),
            TRANSFERHISTORY(TYPE="电视剧", DATE=f"{day_one} 11:00:00"),
            TRANSFERHISTORY(TYPE="动漫", DATE=f"{day_two} 09:00:00"),
            TRANSFERHISTORY(TYPE="电视剧", DATE=f"{day_two} 10:00:00"),
            TRANSFERHISTORY(TYPE="电视剧", DATE=f"{day_two} 11:00:00"),
        ])
        session.commit()

        rows = [tuple(row) for row in DbHelper().get_transfer_statistics(days=5)]

        assert rows == [
            ("电影", day_one, 2),
            ("电视剧", day_one, 1),
            ("动漫", day_two, 1),
            ("电视剧", day_two, 2),
        ]
    finally:
        DbHelper._db = previous_db
        session.close()
        engine.dispose()


@pytest.mark.parametrize("dialect", [postgresql.dialect(), mysql.dialect(), MariaDBDialect()])
def test_transfer_statistics_query_reuses_remote_grouping_expression(dialect):
    engine = sa.create_engine("sqlite:///:memory:")
    session = sessionmaker(bind=engine)()

    class TestDb:
        @staticmethod
        def query(*models):
            return session.query(*models)

    previous_db = DbHelper._db
    DbHelper._db = TestDb()
    try:
        query = DbHelper()._build_transfer_statistics_query("2025-01-01 00:00:00")
        compiled = query.statement.compile(dialect=dialect)

        assert {name for name in compiled.params if name.startswith("substr_")} == {
            "substr_1", "substr_2"
        }
    finally:
        DbHelper._db = previous_db
        session.close()
        engine.dispose()
