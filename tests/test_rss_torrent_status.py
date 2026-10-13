import uuid

from app.db.models import RSSTORRENTS
from app.helper.db_helper import DbHelper


def test_unfinish_deletes_rss_torrent_by_enclosure_when_title_was_normalized():
    helper = DbHelper()
    enclosure = f"https://example.invalid/{uuid.uuid4().hex}.torrent"
    normalized_name = "Example Movie 2026 1080p WEB-DL"
    try:
        helper.simple_insert_rss_torrents(normalized_name, enclosure)
        assert helper._db.query(RSSTORRENTS).filter(
            RSSTORRENTS.ENCLOSURE == enclosure).count() == 1

        helper.simple_delete_rss_torrents("Example Movie", enclosure)
        assert helper._db.query(RSSTORRENTS).filter(
            RSSTORRENTS.ENCLOSURE == enclosure).count() == 0
    finally:
        helper._db.query(RSSTORRENTS).filter(RSSTORRENTS.ENCLOSURE == enclosure).delete()
        helper.release_session()
