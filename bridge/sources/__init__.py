"""bridge.sources：mock 场景与 JSONL 回放（P1.2）+ 真实 codex app-server adapter（P3.1）
+ ZCode 会话观察器（ZC1）。

- mock.py / replay.py：无真实 Codex/网络 IO（固定时钟、确定性 seq）。
- codex.py：锁定版 app-server adapter（P3.1），仅在显式 --live 时启动子进程；
  依赖 bridge/codex_rpc.py（stdio JSON-RPC 客户端）与 bridge/redact.py（脱敏）。
- zcode.py：桌面三真源（rollout/metadata/hook spool）只读观察器（ZC1），
  零网络、零写入；接口契约见该模块 docstring（ZC2 按此接线）。
"""
