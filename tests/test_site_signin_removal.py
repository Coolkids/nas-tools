import json
import unittest
from unittest.mock import patch

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.db.models import MESSAGECLIENT
from app.helper.db_helper import DbHelper


class SiteSigninRemovalTest(unittest.TestCase):
    def test_removes_legacy_site_signin_notification_from_saved_clients(self):
        engine = sa.create_engine("sqlite:///:memory:")
        MESSAGECLIENT.__table__.create(engine)
        session = sessionmaker(bind=engine)()

        class TestDb:
            @staticmethod
            def query(*models):
                return session.query(*models)

        previous_db = DbHelper._db
        DbHelper._db = TestDb()
        try:
            session.add_all([
                MESSAGECLIENT(NAME="with-signin", TYPE="test", SWITCHS=json.dumps(
                    ["site_signin", "download_start"])),
                MESSAGECLIENT(NAME="without-signin", TYPE="test", SWITCHS=json.dumps(
                    ["download_fail"])),
            ])
            session.commit()

            with patch.object(previous_db, "commit"):
                changed = DbHelper().remove_message_client_switch("site_signin")

            self.assertEqual(1, changed)
            self.assertEqual({"download_start", "download_fail"}, {
                name for row in session.query(MESSAGECLIENT).all()
                for name in json.loads(row.SWITCHS)
            })
        finally:
            DbHelper._db = previous_db
            session.close()
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
