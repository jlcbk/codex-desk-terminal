"""bridge.transports.ble — BLE transport 适配器（P3.4，A4-B；ZC10 主机中央）。

当前内容：
  fragmenter        — 冻结帧格式的发送侧分片/接收侧重组内核（纯逻辑，零依赖）。
  central_skeleton  — bleak central 连接/订阅/写 RX 骨架（真机未验证）。
  central           — ZC10 主机侧中央：扫描/连接/MTU/系统配对/分片写 RX/ACK/
                      退避重扫（BleakCentral；服务入口 scripts/bridge_serve_ble.py）。

真源：protocol/transport.md（P0.5 冻结）、docs/INTERFACES.md §7。
红线：Transport 只搬字节——本包不解释业务 JSON、不生成业务状态；
凭证不进入任何帧、日志或 fixture。BLE 包保持纯标准库（bleak 运行期懒加载），
不 import wss 子包（websockets 依赖不传染）。
"""