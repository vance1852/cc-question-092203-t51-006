"""pytest 全局配置：使用独立临时数据库，每个测试后复位时钟。"""
import os
import tempfile

import pytest

# 必须在任何 app.* 模块导入前指定测试数据库
_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["APP_DATABASE_URL"] = f"sqlite:///{_db_path}"

from app.clock import clock  # noqa: E402
from app.seed import init_db  # noqa: E402

init_db()


@pytest.fixture(autouse=True)
def _reset_clock():
    clock.reset()
    yield
    clock.reset()
