"""把唤醒词音频放大并重复，用于板子"自发自收"唤醒测试。

为什么需要它：验证唤醒词最可靠的判据是人对着板子喊，但自动化回归需要
不依赖人。板子喇叭放出的声音要经过空气传到自己的麦克风，衰减很大 ——
直接播原始 TTS 音频（幅度约 -30dB）往往触发不了唤醒词。

这个脚本做两件事：
  1. **增益放大**到接近满量程（默认 0.95），让喇叭输出更响；
  2. **重复若干次**并插入静音间隔，给检测器多次机会。

用法::

    # 生成放大的重复音频（默认 6 次）
    python tools/make_wake_audio_loud.py

    # 自定义
    python tools/make_wake_audio_loud.py --gain 0.98 --repeat 10 --gap-ms 400

产物默认写到 artifacts/wake/wake_loud.wav（16kHz / 16bit / 单声道）。
"""

from __future__ import annotations

import argparse
import array
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_wav_mono16(path: Path) -> tuple[array.array, int]:
    """读入 WAV 并返回 (int16 采样, 采样率)。只接受 16bit 单声道。"""
    with wave.open(str(path), "rb") as w:
        ch = w.getnchannels()
        width = w.getsampwidth()
        rate = w.getframerate()
        frames = w.readframes(w.getnframes())
    if width != 2:
        raise SystemExit(f"需要 16bit WAV，实际 {width * 8}bit：{path}")
    if ch != 1:
        raise SystemExit(f"需要单声道 WAV，实际 {ch} 声道：{path}")

    samples = array.array("h")
    samples.frombytes(frames)
    return samples, rate


def peak_of(samples: array.array) -> int:
    """求峰值绝对值。"""
    return max((abs(s) for s in samples), default=0)


def apply_gain(samples: array.array, gain: float) -> array.array:
    """按目标增益归一化到接近满量程（按当前峰值算，避免削顶）。"""
    peak = peak_of(samples)
    if peak == 0:
        raise SystemExit("音频全为静音，无法放大")
    scale = (gain * 32767.0) / peak
    out = array.array("h", bytes(len(samples) * 2))
    for i, s in enumerate(samples):
        v = int(s * scale)
        if v > 32767:
            v = 32767
        if v < -32768:
            v = -32768
        out[i] = v
    return out


def main() -> int:
    """命令行入口。"""
    p = argparse.ArgumentParser(description="生成放大的重复唤醒词音频")
    p.add_argument("--in", dest="src", default=str(ROOT / "artifacts" / "wake" / "wake_hixiaoxing.wav"))
    p.add_argument("--out", dest="dst", default=str(ROOT / "artifacts" / "wake" / "wake_loud.wav"))
    p.add_argument("--gain", type=float, default=0.95, help="目标峰值占满量程比例 (0~1)")
    p.add_argument("--repeat", type=int, default=6, help="重复次数")
    p.add_argument("--gap-ms", type=int, default=500, help="每次之间的静音间隔 (ms)")
    args = p.parse_args()

    src = Path(args.src)
    if not src.is_file():
        print(f"源文件不存在：{src}")
        print("请先跑：python tools/make_wake_audio.py")
        return 1

    samples, rate = load_wav_mono16(src)
    print(f"源音频: {src.name}  峰值={peak_of(samples)}  时长={len(samples) / rate:.2f}s  采样率={rate}")

    loud = apply_gain(samples, args.gain)
    gap = array.array("h", bytes(int(rate * args.gap_ms / 1000) * 2))

    out = array.array("h")
    for i in range(args.repeat):
        out.extend(loud)
        if i + 1 < args.repeat:
            out.extend(gap)

    dst = Path(args.dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(dst), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(out.tobytes())

    print(f"已生成: {dst}")
    print(f"  峰值={peak_of(out)} (满量程 32767)  时长={len(out) / rate:.1f}s  重复={args.repeat} 次")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
