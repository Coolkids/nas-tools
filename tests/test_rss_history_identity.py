import uuid

from app.db.models import RSSHISTORY
from app.helper.db_helper import DbHelper


def test_movie_and_tv_history_with_same_source_id_are_distinct():
    helper = DbHelper()
    rssid = "p10-history-" + uuid.uuid4().hex
    try:
        helper.insert_rss_history(
            rssid=rssid, rtype="MOV", name="fixture movie", year="2025",
            tmdbid="900000001", image="", desc="movie")
        helper.insert_rss_history(
            rssid=rssid, rtype="MOV", name="duplicate movie", year="2025",
            tmdbid="900000001", image="", desc="duplicate")
        helper.insert_rss_history(
            rssid=rssid, rtype="TV", name="fixture show", year="2025",
            tmdbid="900000002", image="", desc="show", season="S01")

        rows = helper._db.query(RSSHISTORY).filter(
            RSSHISTORY.RSSID == rssid).order_by(RSSHISTORY.ID).all()
        assert [(row.TYPE, row.NAME) for row in rows] == [
            ("MOV", "fixture movie"), ("TV", "fixture show")]
    finally:
        helper._db.query(RSSHISTORY).filter(RSSHISTORY.RSSID == rssid).delete()
        helper._db.commit()
