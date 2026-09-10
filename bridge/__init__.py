"""Codex Desk Terminal Bridge（A1：P1.1 reducer + P1.2 mock/replay）。

模块边界（docs/INTERFACES.md §5）：
- bridge/events.py        NormalizedEvent（bridge 内部事件类型，命名对齐 0.152.0 实测）
- bridge/state/           纯 reducer、裁剪/排序/渲染、StateEngine（无 IO、无墙钟、无随机）
- bridge/sources/         mock 场景与 JSONL 回放（固定时钟、确定性 seq）
- bridge/transports/      归 A4，本包不创建。

本包只依赖 Python 标准库。
"""

__version__ = "0.1.0"
