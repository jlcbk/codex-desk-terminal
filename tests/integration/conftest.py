"""tests/integration conftest — 把仓库根注入 sys.path（bridge.* 可导入）。

与 tests/transport/integration/conftest.py 同模式；本目录（R6）不依赖
C harness，因此不需要 session 级编译 fixture。
"""

from __future__ import annotations

import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
