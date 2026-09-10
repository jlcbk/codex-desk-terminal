"""tests/transport/integration conftest（P3.5，A4+A5）。

- 把仓库根注入 sys.path（bridge.* / p35_common / wss_driver 可导入）；
- session 级 fixture：C 终点 harness 二进制（缺失或源码较新时自动编译，
  参数与 scripts/run_p35.sh 一致）——保证 `pytest tests/transport/integration`
  单独可复跑。
"""

from __future__ import annotations

import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import p35_common  # noqa: E402


@pytest.fixture(scope="session")
def harness_bin() -> str:
    return p35_common.build_harness()
