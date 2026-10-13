from unittest.mock import patch

from app.db.manager import DATABASE
from app.db.migration import _current_settings


def test_remote_settings_are_canonical_for_active_engine_and_maintenance_checks():
    configured = {
        "type": "PostgreSQL",
        "host": "db.example",
        "port": "5432",
        "name": "nastools",
        "username": "app",
        "password": "secret",
    }
    config = type("ConfigStub", (), {
        "get_config": lambda self, key: configured if key == "database" else None,
    })()

    with patch("config.Config", return_value=config):
        active_settings = DATABASE._read_settings()
        maintenance_settings = _current_settings()

    assert active_settings == {
        "type": "postgresql",
        "host": "db.example",
        "port": 5432,
        "name": "nastools",
        "username": "app",
        "password": "secret",
        "connect_timeout": 5,
        "ssl": {},
    }
    assert maintenance_settings == active_settings
    assert configured["type"] == "PostgreSQL"
    assert "ssl" not in configured
