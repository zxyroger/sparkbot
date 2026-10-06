"""串口日志：把设备串口数据通过 WebSocket 推到浏览器。

为什么需要它：
    固件出问题时唯一的线索就是串口输出。以前必须在命令行跑
    ``tools/serial_log.py``，既占着一个终端，也没法一边看日志一边
    用控制台操作设备。这个模块让 Web 控制台直接看串口。

设计要点：

* **串口是独占资源。** 同一时刻只能有一个程序打开 COM15；本模块打开后，
  ``tools/serial_log.py`` 与 ``esp32gw.exe`` 都会失败。所以必须
  提供明确的"打开/关闭"控制，并在占用失败时给出可读的原因。

* **DTR/RTS 必须显式拉低。** ESP32-S3 用这两个信号做自动复位；不设的话，
  打开串口的瞬间电路会拉低 EN 引脚把板子**重启**，即"看一眼日志就把设备
  弄重启了"。这点在 ``tools/serial_log.py`` 里已经踩过。

* **只记录文本行，不记录每个字节。** 串口行是固件的日志行，按行切分后
  每行带时间戳，浏览器端才好显示与过滤。半行（还没收到换行符）先缓存。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

#: 环形缓冲保留的最大行数。够回溯最近几百行日志，又不会吃内存。
DEFAULT_MAX_LINES = 2000

#: 默认串口参数。ESP32-S3 的 USB-Serial/JTAG 固定 115200。
DEFAULT_BAUDRATE = 115200

#: 读超时（秒）。设为 0.2 让读取线程能及时响应停止请求。
_READ_TIMEOUT_S = 0.2


@dataclass
class SerialLine:
    """一行串口输出。"""

    seq: int
    ts: float
    text: str

    def to_dict(self) -> dict[str, Any]:
        """序列化成给前端用的字典。"""
        return {"seq": self.seq, "ts": self.ts, "text": self.text}


@dataclass
class PortInfo:
    """一个可用串口。"""

    device: str
    description: str = ""
    hwid: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {"device": self.device, "description": self.description, "hwid": self.hwid}


def list_ports() -> list[PortInfo]:
    """列出本机串口。pyserial 缺失时返回空列表而不是抛异常。

    串口列表只是辅助信息（用户也可以手填端口名），不该因为它坏了
    就让整个页面报错。
    """
    try:
        from serial.tools import list_ports as _lp
    except Exception:  # noqa: BLE001 - pyserial 未安装
        logger.debug("pyserial 未安装，无法枚举串口")
        return []

    out: list[PortInfo] = []
    for p in _lp.comports():
        out.append(
            PortInfo(
                device=str(p.device),
                description=str(getattr(p, "description", "") or ""),
                hwid=str(getattr(p, "hwid", "") or ""),
            )
        )
    # 常见端口排在前面，方便选择
    out.sort(key=lambda x: (not x.device.upper().startswith("COM"), x.device))
    return out


class SerialLogReader:
    """独占一个串口并把日志行分发给订阅者。

    线程安全：所有状态都在 ``_lock`` 下读写。订阅者回调在**读取线程**里
    以非阻塞方式调用，所以回调必须极快（WebSocket 发送用队列中转）。
    """

    def __init__(self, *, max_lines: int = DEFAULT_MAX_LINES) -> None:
        """初始化。"""
        self._lock = threading.Lock()
        self._lines: deque[SerialLine] = deque(maxlen=max_lines)
        self._seq = 0

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._serial: Any = None

        self._port: str = ""
        self._baudrate: int = DEFAULT_BAUDRATE
        self._last_error: str = ""
        self._rx_bytes = 0

        self._subscribers: list[Callable[[SerialLine], None]] = []

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def status(self) -> dict[str, Any]:
        """当前状态，供 API 与前端显示。"""
        with self._lock:
            return {
                "open": self._serial is not None,
                "port": self._port,
                "baudrate": self._baudrate,
                "lines": len(self._lines),
                "rx_bytes": self._rx_bytes,
                "error": self._last_error,
                # 未安装 pyserial 时前端应给出明确提示，而不是显示"没有串口"
                "pyserial": _has_pyserial(),
            }

    def recent(self, limit: int = 500) -> list[SerialLine]:
        """最近的若干行（按时间正序）。"""
        with self._lock:
            items = list(self._lines)
        if limit > 0:
            items = items[-limit:]
        return items

    def clear(self) -> None:
        """清空缓冲（不影响串口是否打开）。"""
        with self._lock:
            self._lines.clear()

    # ------------------------------------------------------------------ #
    # 订阅
    # ------------------------------------------------------------------ #
    def subscribe(self, cb: Callable[[SerialLine], None]) -> None:
        """注册一个行回调。"""
        with self._lock:
            self._subscribers.append(cb)

    def unsubscribe(self, cb: Callable[[SerialLine], None]) -> None:
        """注销回调。"""
        with self._lock:
            if cb in self._subscribers:
                self._subscribers.remove(cb)

    # ------------------------------------------------------------------ #
    # 串口生命周期
    # ------------------------------------------------------------------ #
    def open(self, port: str, baudrate: int = DEFAULT_BAUDRATE) -> None:
        """打开串口并开始读取。

        Raises:
            RuntimeError: pyserial 缺失、端口被占用或参数非法。
        """
        if not _has_pyserial():
            raise RuntimeError(
                "未安装 pyserial。请执行: python -m pip install pyserial"
            )

        with self._lock:
            if self._serial is not None:
                if self._port == port and self._baudrate == baudrate:
                    return  # 已经是目标状态
                raise RuntimeError(f"串口 {self._port} 已打开，请先关闭")

        try:
            import serial
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"导入 pyserial 失败: {exc}") from exc

        try:
            # 必须显式 dtr=False, rts=False：见模块顶部说明，否则会复位板子。
            ser = serial.Serial()
            ser.port = port
            ser.baudrate = baudrate
            ser.timeout = _READ_TIMEOUT_S
            ser.dtr = False
            ser.rts = False
            ser.open()
            # 打开后再设一次，确保驱动没有在 open() 过程中改动它们
            ser.dtr = False
            ser.rts = False
        except Exception as exc:  # noqa: BLE001 - 串口错误种类多，统一转成可读信息
            hint = ""
            text = str(exc)
            if "Access is denied" in text or "拒绝访问" in text or "PermissionError" in text:
                hint = (
                    "（端口被占用）同一时刻只能有一个程序打开串口。"
                    "请确认没有其它 serial_log.py / 串口监视器 / esp32gw.exe 在运行。"
                )
            with self._lock:
                self._last_error = f"打开 {port} 失败: {text}{hint}"
            raise RuntimeError(self._last_error) from exc

        with self._lock:
            self._serial = ser
            self._port = port
            self._baudrate = baudrate
            self._last_error = ""
            self._rx_bytes = 0
            self._stop.clear()
            # 清掉上次会话残留，避免新旧日志混在一起
            self._lines.clear()

        self._thread = threading.Thread(
            target=self._read_loop, name="serial-log", daemon=True
        )
        self._thread.start()
        logger.info("串口日志已打开: %s @ %d", port, baudrate)

    def close(self) -> None:
        """停止读取并关闭串口。可重复调用。"""
        with self._lock:
            had = self._serial is not None
            self._stop.set()
            ser = self._serial

        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

        if ser is not None:
            try:
                ser.close()
            except Exception:  # noqa: BLE001
                logger.debug("关闭串口时出错（忽略）", exc_info=True)

        with self._lock:
            self._serial = None
            # 清掉端口名：关闭后若还显示 "COM15" 会让人以为端口仍被占用，
            # 而实际上别的程序已经可以用了。历史行与统计保留，便于回看。
            self._port = ""

        if had:
            logger.info("串口日志已关闭")

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def _read_loop(self) -> None:
        """读取线程：按行切分并分发。"""
        buf = bytearray()
        while not self._stop.is_set():
            with self._lock:
                ser = self._serial
            if ser is None:
                break

            try:
                # 一次读一块，再按换行切分；比 readline() 更耐折腾
                # （readline 在串口上遇到超时会返回半行，容易丢数据）
                chunk = ser.read(4096)
            except Exception as exc:  # noqa: BLE001 - 拔线/驱动异常
                with self._lock:
                    self._last_error = f"读取失败: {exc}"
                logger.warning("串口读取失败: %s", exc)
                break
            if not chunk:
                # 读超时（正常空闲）或对端没数据。
                #
                # 这里**必须检查停止标志**：早先的实现直接 continue，
                # 当串口长时间没有数据时这个循环会空转烧 CPU，
                # 而且调用 close() 后也要等下一次循环才退得出来。
                if self._stop.wait(timeout=0.02):
                    break
                continue

            with self._lock:
                self._rx_bytes += len(chunk)

            buf.extend(chunk)

            # 按换行切出完整行；最后一段可能是半行，留到下一轮
            while True:
                idx = buf.find(b"\n")
                if idx < 0:
                    break
                raw = bytes(buf[:idx])
                del buf[: idx + 1]
                self._emit(raw)

            # 防止乱码或没有换行的输出把缓冲撑爆
            if len(buf) > 16384:
                self._emit(bytes(buf))
                buf.clear()

        # 退出前把残留半行也发出去，免得丢最后一行日志
        if buf:
            self._emit(bytes(buf))

    def _emit(self, raw: bytes) -> None:
        """把一行原始字节变成 SerialLine 并分发给订阅者。"""
        # 固件日志是 UTF-8（中文注释），但串口抖动可能产生非法字节；
        # errors="replace" 保证不会因为一行乱码就抛异常。
        text = raw.decode("utf-8", errors="replace").rstrip("\r")

        with self._lock:
            self._seq += 1
            line = SerialLine(seq=self._seq, ts=time.time(), text=text)
            self._lines.append(line)
            subs = list(self._subscribers)

        for cb in subs:
            try:
                cb(line)
            except Exception:  # noqa: BLE001 - 一个订阅者坏了不该影响其它的
                logger.debug("串口行订阅者异常", exc_info=True)


def _has_pyserial() -> bool:
    """pyserial 是否可用。"""
    try:
        import serial  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


# --------------------------------------------------------------------------- #
# 进程级单例
# --------------------------------------------------------------------------- #
_reader: SerialLogReader | None = None
_reader_lock = threading.Lock()


def get_reader() -> SerialLogReader:
    """取得进程级共享的串口读取器。

    用单例是必须的：串口是独占资源，如果每个 WebSocket 连接各自建一个
    reader，第二个连接必然打不开端口。
    """
    global _reader
    with _reader_lock:
        if _reader is None:
            _reader = SerialLogReader()
        return _reader


def stop_reader() -> None:
    """停止共享读取器（服务关闭时调用，释放串口）。"""
    global _reader
    with _reader_lock:
        r = _reader
    if r is not None:
        r.close()
