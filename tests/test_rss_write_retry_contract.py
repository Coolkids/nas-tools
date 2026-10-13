from app.db.main_db import DbPersist
from app.helper.db_helper import DbHelper


def _persist_policy(method):
    for cell in method.__closure__ or ():
        value = cell.cell_contents
        if isinstance(value, DbPersist):
            return value
    raise AssertionError(f"{method.__name__} is not wrapped by DbPersist")


def test_parallel_subscribe_movie_writes_are_safe_to_retry():
    for method in (DbHelper.insert_rss_history,
                   DbHelper.update_rss_movie_state,
                   DbHelper.delete_rss_movie):
        assert _persist_policy(method).retry_safe
