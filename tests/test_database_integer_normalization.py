import pytest

from app.helper.db_helper import DbHelper


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("", None), ("   ", None), ("unknown", None),
     ("12", 12), (-1, -1), (0, 0)],
)
def test_optional_integer_normalizes_blank_values_for_sql_integer_columns(value, expected):
    assert DbHelper._optional_integer(value) == expected
