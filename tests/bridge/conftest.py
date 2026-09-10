"""tests/bridge 共用夹具：schema 校验器、跨字段不变量、字节/深度工具。"""

import json
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "bridge"


def encode(snapshot: dict) -> bytes:
    """与 bridge 渲染一致的紧凑编码（字节预算检查用）。"""
    return json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def depth(obj) -> int:
    if isinstance(obj, dict):
        return 1 + max([depth(v) for v in obj.values()] or [0])
    if isinstance(obj, list):
        return 1 + max([depth(v) for v in obj] or [0])
    return 0


def assert_invariants(snap: dict) -> None:
    """schema 无法表达的跨字段一致性（protocol/state.schema.json 描述第 3 条）。"""
    assert snap["threads_total"] >= len(snap["threads"])
    assert snap["threads_total"] <= 65535
    assert len(snap["threads"]) <= 8
    usage = snap["usage"]
    assert usage["windows_total"] >= len(usage["windows"])
    assert len(usage["windows"]) <= 4
    ids = [t["id"] for t in snap["threads"]]
    assert len(ids) == len(set(ids)), "thread ids must be unique"
    assert snap["selected_thread_id"] is None or snap["selected_thread_id"] in ids
    assert len(encode(snap)) <= 16384, "snapshot must fit 16KiB"
    assert depth(snap) <= 12, "snapshot depth must be <= 12"


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
