"""ZC10 服务入口测试：scripts/bridge_serve_ble.py（主机侧编排，无设备）。

- --dry-run 冒烟：退出 0，打印冻结 UUID 与流程摘要（无设备环境可用）；
- 编排复用契约：公共逻辑（PublishBridge/make_sink/initial_snapshot/
  observer_worker）import 自 bridge_serve_zcode.py，不复制（serve_zcode
  本体不许碰，见任务书红线）；
- 脚本自身不直接触碰 bleak 扫描/连接 API（经 BleakCentral 懒加载封装），
  dry-run 路径不构造 BLE 对象。
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

SCRIPT_PATH = REPO / "scripts" / "bridge_serve_ble.py"

FROZEN_SERVICE_UUID = "B931F216-B7FD-50E9-8C33-F1416ADE3B1D"
FROZEN_RX_UUID = "890AAC2C-3C19-5200-BDEC-74943BA536A1"
FROZEN_TX_UUID = "8EABE170-A4E7-5C26-A287-6F5871014707"


def load_script():
    spec = importlib.util.spec_from_file_location("bridge_serve_ble_script",
                                                  SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_dry_run_exit_zero_prints_contract():
    """--dry-run：退出 0；打印冻结 UUID、退避序列与流程；无 token 项
    （BLE 配对加密即信道安全，无凭证输出）。"""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--dry-run",
         "--device-name", "CodexDT"],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "CDT_HOOK_SPOOL": "/tmp/zc10_dry_spool.jsonl"})
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "dry-run PASS" in out
    for frozen in (FROZEN_SERVICE_UUID, FROZEN_RX_UUID, FROZEN_TX_UUID):
        assert frozen in out, frozen
    assert "device_filter: name=CodexDT" in out
    assert "1/2/4/8/16/30" in out  # §5.4 退避序列
    assert "chunk = ATT_MTU" in out  # §2.4 片容量算式
    # BLE 服务无 token/证书配置项（配对加密=信道安全；§2.3）
    assert "--token" not in out and "token_file" not in out


def test_script_reuses_zcode_serve_orchestration():
    """公共逻辑 import 自 serve_zcode（单一来源，不复制不另立 backlog）。"""
    script = load_script()
    zc = script.ZC
    for attr in ("PublishBridge", "make_sink", "initial_snapshot",
                 "observer_worker", "resolve_spool_path"):
        assert callable(getattr(zc, attr)), attr
    # 本脚本不得自定义这些编排件（防复制漂移）
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    for forbidden in ("def make_sink", "def initial_snapshot",
                      "def observer_worker", "class PublishBridge"):
        assert forbidden not in source, forbidden


def test_script_never_touches_bleak_directly():
    """脚本不直接调用 bleak API（扫描/连接封装在 BleakCentral 懒加载内，
    dry-run 不构造 BLE 对象、不触发系统蓝牙）。"""
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    for forbidden in ("BleakScanner", "BleakClient("):
        assert forbidden not in source, forbidden
    dry_body = source.split("def run_dry_run")[1].split("\ndef ")[0]
    assert "BleakCentral(" not in dry_body


def test_parser_defaults_and_args():
    script = load_script()
    parser = script.build_parser()
    args = parser.parse_args([])
    assert args.device_name == "CodexDT"
    assert args.address is None
    assert args.dry_run is False
    assert args.poll_interval == 1.0
    # 观察器侧参数与 serve_zcode 同口径（observer_worker 依赖这些属性名）
    for attr in ("rollout_dir", "agents_dir", "spool", "poll_interval"):
        assert hasattr(args, attr), attr
