"""bridge.transports.ble — BLE transport 适配器（P3.4，A4-B）。

当前内容：
  fragmenter        — 冻结帧格式的发送侧分片/接收侧重组内核（纯逻辑，零依赖）。
  central_skeleton  — bleak central 连接/订阅/写 RX 骨架（真机未验证）。

真源：protocol/transport.md（P0.5 冻结）、docs/INTERFACES.md §7。
红线：Transport 只搬字节——本包不解释业务 JSON、不生成业务状态；
凭证不进入任何帧、日志或 fixture。
"""
