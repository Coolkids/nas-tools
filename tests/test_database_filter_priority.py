"""过滤规则的旧文本优先级按稳定数值顺序读取。"""

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.db.models import CONFIGFILTERRULES
from app.helper.db_helper import DbHelper


def test_filter_rule_priority_orders_numeric_values_and_legacy_text_stably():
    engine = sa.create_engine("sqlite:///:memory:")
    CONFIGFILTERRULES.__table__.create(engine)
    session = sessionmaker(bind=engine)()

    class TestDb:
        @staticmethod
        def query(*models):
            return session.query(*models)

    previous_db = DbHelper._db
    DbHelper._db = TestDb()
    try:
        session.add_all([
            CONFIGFILTERRULES(ID=7, GROUP_ID="1", PRIORITY="10"),
            CONFIGFILTERRULES(ID=2, GROUP_ID="1", PRIORITY="2"),
            CONFIGFILTERRULES(ID=5, GROUP_ID="1", PRIORITY=None),
            CONFIGFILTERRULES(ID=4, GROUP_ID="1", PRIORITY=""),
            CONFIGFILTERRULES(ID=6, GROUP_ID="1", PRIORITY="Z"),
            CONFIGFILTERRULES(ID=3, GROUP_ID="1", PRIORITY="alpha"),
            CONFIGFILTERRULES(ID=1, GROUP_ID="2", PRIORITY="0"),
        ])
        session.commit()

        rules = DbHelper().get_config_filter_rule(groupid="1")

        assert [rule.ID for rule in rules] == [4, 5, 2, 7, 3, 6]
    finally:
        DbHelper._db = previous_db
        session.close()
        engine.dispose()
