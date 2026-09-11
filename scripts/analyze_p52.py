#!/usr/bin/env python3
"""analyze_p52.py — P5.2 前半连接稳定性 A/B 数据分析（只读日志，出 JSON 摘要）。

输入：
  bridge 日志（bridge_serve_mock.py 输出，含 tls-relay/pump/keepalive 行）
  串口日志  （serial_peek.py 捕获的设备口输出）
输出（stdout, JSON）：
  bridge: relay_closes（非人为断链数）、pump_up_timeouts、ping_rtt_ms（n/median/max）、
          keepalive 最大间隔、relay 主动 FIN（pump_up_timeout 后 0.5s 内 close）
  serial: wss 断开/重连/升级次数、applied 快照数、panic/Guru/abort 计数、
          dev_net PS 模式行、设备最后读间隔（mbedtls f_recv 时间戳粗粒度）

用法：python3 scripts/analyze_p52.py <bridge.log> [serial.log]
凭证红线：只解析日志行，不接触 token/证书文件。
"""
from __future__ import annotations

import json
import re
import statistics
import sys
from datetime import datetime

BRIDGE_RE = re.compile(
    r"^(2026-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) INFO .*"
    r"pump\((up|tls)>\('([\d.]+)', (\d+)\)\) \+ (\d+) bytes (.*)$"
)


def parse_bridge(path: str, since: str | None = None) -> dict:
    pings: dict[int, datetime] = {}
    rtts: list[float] = []
    last_to_dev: datetime | None = None
    max_gap_to = 0.0
    pump_up_timeouts = 0
    relay_closes = 0
    closes_after_timeout = 0
    prev_timeout_ts: datetime | None = None
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    since_ts = None
    if since:
        since_ts = datetime.strptime(since, "%Y-%m-%d %H:%M:%S")
    for raw in open(path, encoding="utf-8", errors="replace"):
        m = BRIDGE_RE.match(raw)
        ts = None
        hm = re.match(r"^(2026-\d\d-\d\d \d\d:\d\d:\d\d,\d{3})", raw)
        if hm:
            ts = datetime.strptime(hm.group(1), "%Y-%m-%d %H:%M:%S,%f")
            if since_ts is not None and ts < since_ts:
                continue  # 腿边界过滤：只统计 since 之后的事件
            if first_ts is None:
                first_ts = ts
            last_ts = ts
        if m:
            side, port, nb, py = m.group(2), int(m.group(4)), int(m.group(5)), m.group(6)
            if side == "up":
                if last_to_dev is not None:
                    max_gap_to = max(max_gap_to, (ts - last_to_dev).total_seconds())
                last_to_dev = ts
                is_ping = py.startswith("b'\\x89") or py.startswith('b"\\x89')
                if is_ping:
                    pings[port] = ts
            else:
                is_pong = py.startswith("b'\\x8a") or py.startswith('b"\\x8a')
                if is_pong and port in pings:
                    rtts.append((ts - pings.pop(port)).total_seconds() * 1000.0)
        if "OSError timed out (total" in raw and "pump(up>" in raw:
            pump_up_timeouts += 1
            prev_timeout_ts = ts
        if re.search(r"tls-relay \('.*', \d+\) closed", raw):
            relay_closes += 1
            if prev_timeout_ts is not None and ts is not None and (ts - prev_timeout_ts).total_seconds() < 1.0:
                closes_after_timeout += 1
            prev_timeout_ts = None
    dur = (last_ts - first_ts).total_seconds() if first_ts and last_ts else 0.0
    rt = sorted(rtts)
    return {
        "window_s": round(dur, 1),
        "relay_closes": relay_closes,
        "relay_closes_caused_by_pump_up_timeout": closes_after_timeout,
        "pump_up_timeouts": pump_up_timeouts,
        "ping_pong_pairs": len(rt),
        "ping_rtt_ms_median": round(statistics.median(rt), 1) if rt else None,
        "ping_rtt_ms_max": round(rt[-1], 1) if rt else None,
        "rtt_over_3000ms": sum(1 for v in rt if v > 3000.0),
        "max_gap_server_to_dev_s": round(max_gap_to, 1),
    }


SERIAL_PATTERNS = {
    "wss_error_events": r"cdt_wss: 错误事件",
    "wss_disconnects": r"cdt_wss: 断开 close=",
    "wss_upgrades": r"cdt_wss: 升级成功",
    "sta_disconnects": r"dev_net.*断开（第",
    "applied": r"\[state\] applied #",
    "panics": r"Guru Meditation|panic|abort\(\)",
    "reboots": r"rst:",
}


def parse_serial(path: str) -> dict:
    out: dict[str, int] = {k: 0 for k in SERIAL_PATTERNS}
    ps_line = None
    upgrade_ms: list[int] = []
    prev_ms: int | None = None
    max_apply_gap = 0
    for raw in open(path, encoding="utf-8", errors="replace"):
        for key, pat in SERIAL_PATTERNS.items():
            if re.search(pat, raw):
                out[key] += 1
        if "WiFi PS 模式" in raw:
            ps_line = raw.strip()[:160]
        hm = re.search(r"\[state\] applied #\d+ .*bytes=(\d+)", raw)
        tm = re.match(r".*I \((\d+)\) ", raw)
        if tm:
            ms = int(tm.group(1))
            if "升级成功" in raw and prev_ms is not None:
                pass
            if hm and prev_ms is not None:
                # applied 间隔仅在连续 applied 之间统计（含 keepalive 15s 档）
                max_apply_gap = max(max_apply_gap, ms - prev_ms)
            if hm:
                prev_ms = ms
    out["wifi_ps_mode_line"] = ps_line
    out["max_gap_between_applied_ms"] = max_apply_gap
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    since = None
    if "--since" in sys.argv:
        i = sys.argv.index("--since")
        since = sys.argv[i + 1]
        sys.argv = sys.argv[:i] + sys.argv[i + 2:]
    result = {"bridge": parse_bridge(sys.argv[1], since)}
    if len(sys.argv) > 2:
        result["serial"] = parse_serial(sys.argv[2])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
