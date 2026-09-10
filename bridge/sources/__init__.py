"""bridge.sources：mock 场景与 JSONL 回放（P1.2）+ 真实 codex app-server adapter（P3.1）。

- mock.py / replay.py：无真实 Codex/网络 IO（固定时钟、确定性 seq）。
- codex.py：锁定版 app-server adapter（P3.1），仅在显式 --live 时启动子进程；
  依赖 bridge/codex_rpc.py（stdio JSON-RPC 客户端）与 bridge/redact.py（脱敏）。
"""
