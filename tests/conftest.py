"""共享测试初始化逻辑，避免测试使用开发者的 NAS 数据库。"""

import os
import tempfile

import pytest


_config_path = os.path.join(tempfile.gettempdir(), "nas-tools-pytest-config.yaml")
os.environ.setdefault("NASTOOL_CONFIG", _config_path)


@pytest.fixture(scope="session", autouse=True)
def initialize_test_databases():
    from app.db import init_db

    init_db()
