#!/usr/bin/env python3
"""tmp_r34_serial.py — 烧录验证轮（A3+A4）临时串口日志工具，随轮收编后可删。

只读监听 /dev/cu.usbmodem1301（115200 8N1），每行加本地墙钟时间戳，
原样写入证据文件。复用 artifacts/board/serial_peek.py 的打开方式：
O_RDONLY | O_NOCTTY | O_NONBLOCK，不翻转 DTR/RTS（运行中附加不复位设备），
不向设备写任何数据。一次只允许一个监听者（本轮红线）。

用法：python3 scripts/tmp_r34_serial.py SECONDS OUT_LOG [PORT]
"""
import os
import select
import sys
import termios
import time

PORT = "/dev/cu.usbmodem1301"
DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
LOG = sys.argv[2] if len(sys.argv) > 2 else "serial.log"
PORT = sys.argv[3] if len(sys.argv) > 3 else PORT

fd = os.open(PORT, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
try:
    attrs = termios.tcgetattr(fd)
    attrs[0] = 0
    attrs[1] = 0
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    attrs[3] = 0
    attrs[4] = termios.B115200
    attrs[5] = termios.B115200
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attrs)

    start = time.time()
    print(f"[tmp_r34_serial] open {PORT} read-only for {DURATION:.0f}s -> {LOG}",
          flush=True)
    buf = b""

    def flush_line(line: bytes) -> None:
        now = time.strftime("%H:%M:%S") + f".{int((time.time() % 1) * 1000):03d}"
        with open(LOG, "ab") as out:
            out.write(f"[{now}] ".encode() + line + b"\n")

    while time.time() - start < DURATION:
        r, _, _ = select.select([fd], [], [], 0.25)
        if not r:
            continue
        try:
            chunk = os.read(fd, 8192)
        except OSError as exc:
            # macOS USB-CDC 偶发瞬时读错误：短退避后重开端口继续（不丢已收数据）
            print(f"[tmp_r34_serial] read OSError {exc}，1s 后重开端口", flush=True)
            time.sleep(1.0)
            try:
                os.close(fd)
            except OSError:
                pass
            time.sleep(0.5)
            fd = os.open(PORT, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
            attrs = termios.tcgetattr(fd)
            attrs[0] = 0
            attrs[1] = 0
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[3] = 0
            attrs[4] = termios.B115200
            attrs[5] = termios.B115200
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            continue
        buf += chunk
        if not chunk:
            # EOF（设备 CDC 短暂重枚举）：退避重开
            time.sleep(1.0)
            try:
                os.close(fd)
            except OSError:
                pass
            fd = os.open(PORT, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            flush_line(line)
    if buf:
        flush_line(buf)
    print(f"[tmp_r34_serial] done -> {LOG}", flush=True)
finally:
    os.close(fd)
