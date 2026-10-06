"""串口日志模块的自动化测试。

不依赖真实硬件：用假串口对象验证**环形缓冲、订阅分发、行切分、HTML 渲染
用的状态字段**这些逻辑。真实串口只能在有板子时手测。

跑法::

    python tests/test_serial_log.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".vendor"))

from sparkbot import serial_log as sl  # noqa: E402

_passed = 0
_failed: list[str] = []


def check(cond: bool, label: str) -> None:
    """记录一条断言。"""
    global _passed
    if cond:
        _passed += 1
    else:
        _failed.append(label)
        print(f"  [FAIL] {label}")


class FakeSerial:
    """假的串口对象：把预设字节按块吐出来。

    数据吐完后 ``read()`` 返回空字节（模拟读超时）。读取循环遇到空数据会
    ``_stop.wait(0.02)`` 而不是忙等，所以测试里要记得在数据消费完后
    ``set()`` 停止标志，否则循环会一直等下去。
    """

    def __init__(self, chunks: list[bytes], *, keep_open: bool = False) -> None:
        """初始化。"""
        self._chunks = list(chunks)
        self._keep_open = keep_open
        self.closed = False
        self.dtr = True
        self.rts = True

    def read(self, _n: int) -> bytes:
        """返回下一块数据；用完后空转（模拟串口空闲）。"""
        if self._chunks:
            return self._chunks.pop(0)
        if self._keep_open:
            time.sleep(0.01)
            return b""
        return b""

    def close(self) -> None:
        """标记关闭。"""
        self.closed = True


def drain(r: "sl.SerialLogReader", chunks: list[bytes]) -> list[str]:
    """把预设数据喂给读取循环并返回收到的行。

    在后台线程跑 ``_read_loop``，主线程等数据消费完就 ``_stop.set()``，
    这样循环能自然退出，不会挂住测试。
    """
    r._serial = FakeSerial(list(chunks))
    r._port = "FAKE"
    r._stop.clear()

    got: list[str] = []
    r.subscribe(lambda ln: got.append(ln.text))

    t = threading.Thread(target=r._read_loop, daemon=True)
    t.start()

    # 等所有块被取走（最多 2 秒），再让循环退出
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not r._serial._chunks:  # noqa: SLF001 - 测试里直接看假对象
            break
        time.sleep(0.01)
    time.sleep(0.05)  # 给剩余字节一点处理时间
    r._stop.set()
    t.join(timeout=2.0)
    return got


def test_line_splitting() -> None:
    """按换行切分，半行留到下一批。"""
    print("行切分")

    r = sl.SerialLogReader()
    got = drain(r, [b"line one\nline", b" two\nline three\n"])
    check(
        got == ["line one", "line two", "line three"],
        f"半行应拼接到下一批（得到 {got!r}）",
    )

    # 结尾没有换行的残留半行也必须发出来，否则会丢最后一条日志
    r2 = sl.SerialLogReader()
    got2 = drain(r2, [b"a\n", b"tail-no-newline"])
    check(
        got2 == ["a", "tail-no-newline"],
        f"结尾无换行的半行不应丢失（得到 {got2!r}）",
    )


def test_crlf_stripped() -> None:
    """CRLF 的 \\r 要去掉，否则前端每行尾多一个回车符。"""
    print("CRLF")
    r = sl.SerialLogReader()
    got = drain(r, [b"hello\r\n"])
    check(got == ["hello"], f"\\r 应被去掉（得到 {got!r}）")


def test_ring_buffer() -> None:
    """环形缓冲超上限后丢最旧的。"""
    print("环形缓冲")
    r = sl.SerialLogReader(max_lines=5)
    r._stop.clear()
    for i in range(10):
        r._emit(f"line{i}".encode())
    # 必须传 limit=0：recent() 默认只返回最近 500 条，用默认值会让
    # "到底保留了多少行"这个被测事实看不出来（这个坑我踩过一次）。
    lines = r.recent(0)
    check(len(lines) == 5, f"应只保留 5 行（得到 {len(lines)}）")
    check(lines[-1].text == "line9", "最后一行应是 line9")
    check(lines[0].text == "line5", "最旧的一行应是 line5")

    # recent(limit) 取最近的 N 条，且保持时间正序
    last2 = r.recent(2)
    check([x.text for x in last2] == ["line8", "line9"],
          f"recent(2) 应返回最后两条（得到 {[x.text for x in last2]!r}）")


def test_recent_default_limit() -> None:
    """recent() 的默认 limit 只影响返回值，不影响真实总量。"""
    print("recent 默认上限")
    r = sl.SerialLogReader(max_lines=5000)
    for i in range(1000):
        r._emit(str(i).encode())
    check(len(r.recent(0)) == 1000, "limit=0 应返回全部 1000 条")
    check(len(r.recent()) == 500, "默认 limit 应只返回最近 500 条")
    check(r.status()["lines"] == 1000, "status 里的 lines 应是真实总量 1000")


def test_seq_monotonic() -> None:
    """序号单调递增，前端可用来定位。"""
    print("序号")
    r = sl.SerialLogReader()
    for i in range(3):
        r._emit(f"x{i}".encode())
    seqs = [ln.seq for ln in r.recent()]
    check(seqs == [1, 2, 3], f"序号应是 1,2,3（得到 {seqs!r}）")


def test_clear() -> None:
    """清空只影响缓冲。"""
    print("清空")
    r = sl.SerialLogReader()
    r._emit(b"a")
    r._emit(b"b")
    r.clear()
    check(r.recent() == [], "清空后应为空")


def test_subscribe_unsubscribe() -> None:
    """订阅者能收到行；注销后不再收到。"""
    print("订阅")
    r = sl.SerialLogReader()
    got: list[str] = []
    cb = lambda ln: got.append(ln.text)  # noqa: E731

    r.subscribe(cb)
    r._emit(b"one")
    check(got == ["one"], f"订阅者应收到 one（得到 {got!r}）")

    r.unsubscribe(cb)
    r._emit(b"two")
    check(got == ["one"], "注销后不应再收到")


def test_bad_subscriber_isolated() -> None:
    """一个订阅者抛异常不该影响其它订阅者，也不该打断串口读取。"""
    print("订阅者隔离")
    r = sl.SerialLogReader()
    good: list[str] = []

    def bad(_ln: object) -> None:
        raise RuntimeError("boom")

    r.subscribe(bad)
    r.subscribe(lambda ln: good.append(ln.text))
    r._emit(b"ok")
    check(good == ["ok"], f"坏订阅者不应影响好订阅者（得到 {good!r}）")


def test_invalid_utf8_replaced() -> None:
    """非法 UTF-8 字节不该抛异常（串口抖动很常见）。"""
    print("非法 UTF-8")
    r = sl.SerialLogReader()
    r._emit(b"\xff\xfe bad \x80 bytes")
    lines = r.recent()
    check(len(lines) == 1, "应产生一行而不是抛异常")
    check("bad" in lines[0].text, "可读部分应保留")


def test_status_shape() -> None:
    """status() 的字段是前端依赖的契约。"""
    print("状态字段")
    r = sl.SerialLogReader()
    st = r.status()
    for key in ("open", "port", "baudrate", "lines", "rx_bytes", "error", "pyserial"):
        check(key in st, f"status 应含 {key}")
    check(st["open"] is False, "初始应为未打开")
    check(st["baudrate"] == sl.DEFAULT_BAUDRATE, "默认波特率应是 115200")


def test_open_without_pyserial() -> None:
    """pyserial 缺失时给出可读错误，而不是 AttributeError。"""
    print("pyserial 缺失")
    r = sl.SerialLogReader()
    original = sl._has_pyserial
    try:
        sl._has_pyserial = lambda: False  # type: ignore[assignment]
        try:
            r.open("COM1")
            check(False, "应当抛 RuntimeError")
        except RuntimeError as exc:
            check("pyserial" in str(exc), f"错误信息应提到 pyserial（得到 {exc}）")
    finally:
        sl._has_pyserial = original  # type: ignore[assignment]


def test_singleton() -> None:
    """get_reader 必须返回同一个实例（串口独占）。"""
    print("单例")
    a = sl.get_reader()
    b = sl.get_reader()
    check(a is b, "两次 get_reader 应返回同一对象")
    check(isinstance(a, sl.SerialLogReader), "类型应是 SerialLogReader")


def test_thread_safety() -> None:
    """多线程并发 emit 时序号不重复、行数正确。"""
    print("并发")
    r = sl.SerialLogReader(max_lines=10000)
    n_threads, per = 4, 250

    def worker(tag: str) -> None:
        for i in range(per):
            r._emit(f"{tag}-{i}".encode())

    threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = r.recent(0)
    check(len(lines) == n_threads * per, f"总行数应是 {n_threads * per}（得到 {len(lines)}）")
    seqs = {ln.seq for ln in lines}
    check(len(seqs) == len(lines), "序号不应重复")


def main() -> int:
    """跑全部用例。"""
    print("=" * 62)
    print("串口日志模块测试")
    print("=" * 62)
    print()

    test_line_splitting()
    test_crlf_stripped()
    test_ring_buffer()
    test_recent_default_limit()
    test_seq_monotonic()
    test_clear()
    test_subscribe_unsubscribe()
    test_bad_subscriber_isolated()
    test_invalid_utf8_replaced()
    test_status_shape()
    test_open_without_pyserial()
    test_singleton()
    test_thread_safety()

    print()
    print("=" * 62)
    total = _passed + len(_failed)
    print(f"断言汇总: {_passed}/{total} 通过")
    if _failed:
        print()
        print("失败项:")
        for f in _failed:
            print(f"  - {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
