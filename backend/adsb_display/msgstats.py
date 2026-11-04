"""DF17 报文统计器：直连 readsb raw 输出端口（--net-ro-port，beast/raw 格式），
逐帧解析并独立校验 Mode-S CRC-24，仅统计 DF=17（ADS-B ES）且 CRC 校验通过的报文。

readsb 的 aircraft.json `messages` 字段统计所有 DF 类型（DF11/DF17/DF20/21 等），
无法区分报文类型；raw 端口输出的每条报文自带 DF 与奇偶校验位，后端自行校验 CRC
即得到"实际采集到的、CRC 正确的 ADS-B 报文数"。

raw 端口每行格式（readsb 3.x，AVR 格式）：
  "*" + hex 字符 + ";"，hex 为 7/14 字节报文（14/28 个 hex 字符），部分构建会在
  末尾附加 2 个 hex 字符的信号强度；解析时兼容两种形式。
"""
from __future__ import annotations

import logging
import socket
import threading
import time

log = logging.getLogger("adsb.msgstats")

# Mode-S CRC-24 多项式（生成式 0xFFF409，W. H. Blanchard 约定）
_CRC_POLY = 0xFFF409
_CRC_TABLE = []
for _i in range(256):
    _c = _i << 16
    for _ in range(8):
        _c = ((_c << 1) ^ _CRC_POLY) & 0xFFFFFF if (_c & 0x800000) else (_c << 1) & 0xFFFFFF
    _CRC_TABLE.append(_c)


def mode_s_crc(data: bytes) -> int:
    """计算 Mode-S 报文（5/7/14 字节）的 24 位 CRC。"""
    crc = 0
    for b in data:
        crc ^= b << 16
        for _ in range(8):
            if crc & 0x800000:
                crc = ((crc << 1) ^ _CRC_POLY) & 0xFFFFFF
            else:
                crc = (crc << 1) & 0xFFFFFF
    return crc


def df17_crc_ok(data: bytes) -> bool:
    """DF17 且 CRC 校验通过。

    ADS-B（DF17）报文采用无地址字段的奇偶校验，接收到的校验位应为 0（对整帧
    计算 CRC 得 0 即通过）；其它 DF 类型直接判否。
    """
    if len(data) != 14:
        return False
    df = data[0] >> 3
    if df != 17:
        return False
    return mode_s_crc(data) == 0


class Df17Counter:
    """连接 readsb raw 端口的后台线程，累计 DF17 CRC 通过报文数。

    连接断开自动重连（readsb 未就绪/重启时静默重试）；计数在重连后不清零
    （语义为本次采集会话累计值）。readsb raw 输出本身只含 CRC 通过的报文，
    后端仍独立复算 CRC，保证"实际采集 + 校验成功"的口径完全自证。
    """

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.messages_total = 0
        self.connected = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="df17-counter", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def status(self) -> dict:
        return {"messages_df17": self.messages_total, "raw_connected": self.connected}

    # ------------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._consume()
            except (OSError, ValueError) as e:
                if self.connected:
                    log.info("raw 端口连接断开，3 秒后重连: %s", e)
                self.connected = False
                self._stop.wait(3.0)

    def _consume(self) -> None:
        with socket.create_connection((self.host, self.port), timeout=5) as sock:
            sock.settimeout(1.0)
            self.connected = True
            log.info("已连接 readsb raw 端口 %s:%d，开始统计 DF17 报文", self.host, self.port)
            buf = b""
            try:
                while not self._stop.is_set():
                    try:
                        chunk = sock.recv(65536)
                    except socket.timeout:
                        continue
                    if not chunk:
                        raise OSError("连接被对端关闭")
                    buf += chunk
                    while True:
                        end = buf.find(b";")
                        if end < 0:
                            # 防御：异常数据流无分号时避免缓冲无限增长
                            if len(buf) > 1 << 20:
                                buf = buf[-64:]
                            break
                        frame, buf = buf[:end], buf[end + 1:]
                        self._handle_frame(frame)
            finally:
                self.connected = False

    def _handle_frame(self, frame: bytes) -> None:
        frame = frame.strip(b" \t\r\n;")  # 只剥空白与分号；前导 * 是帧起始标志，必须保留
        if not frame.startswith(b"*") or len(frame) < 2:
            return
        hexpart = frame[1:]
        # 兼容 "*<报文hex>*<RSSI>" 形式（内部第二个 * 后为 2 位信号强度）
        star = hexpart.find(b"*")
        if star >= 0:
            hexpart = hexpart[:star]
        try:
            data = bytes.fromhex(hexpart.decode("ascii"))
        except ValueError:
            return
        # 兼容末尾附加 1 字节信号强度（15 字节报文）
        if len(data) == 15:
            data = data[:14]
        if df17_crc_ok(data):
            self.messages_total += 1
