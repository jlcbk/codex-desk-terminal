"""tests/bridge 共用夹具：schema 校验器 fixture 定义。

共享工具（REPO_ROOT/encode/depth/assert_invariants/FrozenZcodeWallClock）已迁至
唯一命名模块 `cdt_bridge_shared`——pytest prepend 模式下多目录 conftest 同名
竞态（A0 收编 2026-09-13，见 cdt_bridge_shared.py 文档），裸 `from conftest
import ...` 不可依赖。本文件保留：pytest fixture 定义 + 向后兼容的再导出。
"""

import json
import pathlib
import sys

import pytest

from cdt_bridge_shared import (  # noqa: F401  （再导出，兼容既有 from cdt_bridge_shared import）
    REPO_ROOT,
    assert_invariants,
    depth,
    encode,
)

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "bridge"


@pytest.fixture(scope="session")
def validator():
    from jsonschema.validators import Draft202012Validator

    schema_path = REPO_ROOT / "protocol" / "state.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


@pytest.fixture(scope="session")
def lifecycle_fixtures():
    """每个 mock 场景的全部快照（每测试文件内只构建一次）。"""
    from bridge.sources import mock

    return {name: mock.run(name) for name in mock.SCENARIO_NAMES}
