"""实测本地 SenseVoice：准确率 + 延迟。

做法：用 Windows 中文语音合成若干句**已知文本**的音频，再让 SenseVoice
识别回来，对比原文 —— 这样准确率和延迟都是可量化的，不靠感觉。

注意：TTS 合成的语音比真人更"干净"，识别率会**偏高**。所以这里的数字
用于判断"跑得动吗、快不快"，真实场景的识别率要以对着板子说话为准。

用法::

    D:\\dsh\\sparkbot\\speech-service\\.venv\\Scripts\\python.exe benchmark.py
    D:\\dsh\\sparkbot\\speech-service\\.venv\\Scripts\\python.exe benchmark.py --url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import time
import urllib.request
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "bench"

#: 测试语料：覆盖日常对话、数字、专有名词（唤醒词/设备名）
SENTENCES = [
    "你好，你能听见我说话吗",
    "桌子上有一个红色的杯子",
    "请向前走两米然后停下来",
    "现在几点了",
    "今天天气怎么样",
    "帮我看看前面有什么东西",
    "请把屏幕上的字改成你好世界",
    "电量还剩百分之八十",
]

_PS_SYNTH = r"""
param([string]$Text, [string]$OutPath, [string]$VoiceName)
Add-Type -AssemblyName System.Speech
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
    16000,
    [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
    [System.Speech.AudioFormat.AudioChannel]::Mono)
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
if ($VoiceName) { $synth.SelectVoice($VoiceName) }
$synth.SetOutputToWaveFile($OutPath, $fmt)
$synth.Speak($Text)
$synth.Dispose()
Write-Output "OK"
"""


def pick_chinese_voice() -> str | None:
    """挑一个可用的中文语音。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Add-Type -AssemblyName System.Speech; "
             "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
             "$s.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name + '|' + $_.VoiceInfo.Culture }"],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:  # noqa: BLE001
        return None
    for line in (out.stdout or "").splitlines():
        if "|" in line:
            name, culture = line.strip().split("|", 1)
            if culture.lower().startswith("zh"):
                return name
    return None


def synth(text: str, path: Path, voice: str | None) -> bool:
    """合成一句到 WAV（16kHz/16bit/单声道）。"""
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as f:
        f.write(_PS_SYNTH)
        script = f.name
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", script, "-Text", text, "-OutPath", str(path), "-VoiceName", voice or ""],
            capture_output=True, text=True, timeout=120,
        )
    finally:
        Path(script).unlink(missing_ok=True)
    return "OK" in (out.stdout or "") and path.is_file() and path.stat().st_size > 1000


def post_audio(url: str, wav_bytes: bytes, timeout: float = 180.0) -> dict:
    """以 multipart 形式 POST 到 /v1/audio/transcriptions。"""
    boundary = "----sparkbotbench"
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="file"; filename="a.wav"\r\n',
        b"Content-Type: audio/wav\r\n\r\n", wav_bytes, b"\r\n",
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="model"\r\n\r\n', b"sensevoice\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(
        f"{url.rstrip('/')}/audio/transcriptions",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Authorization": "Bearer local"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def normalize(text: str) -> str:
    """去掉标点与空白，只比字。"""
    return re.sub(r"[\s，。、？！,\.\?!]+", "", text)


def cer(ref: str, hyp: str) -> float:
    """字错率（编辑距离 / 参考长度）。中文按字计。"""
    r, h = normalize(ref), normalize(hyp)
    if not r:
        return 0.0 if not h else 1.0
    prev = list(range(len(h) + 1))
    for i, rc in enumerate(r, 1):
        cur = [i]
        for j, hc in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rc != hc)))
        prev = cur
    return prev[-1] / len(r)


def wav_duration(b: bytes) -> float:
    """WAV 时长（秒）。"""
    with wave.open(io.BytesIO(b), "rb") as w:
        return w.getnframes() / w.getframerate()


def main() -> int:
    """命令行入口。"""
    p = argparse.ArgumentParser(description="实测本地 SenseVoice")
    p.add_argument("--url", default="http://127.0.0.1:8000")
    p.add_argument("--voice", default=None, help="TTS 语音名，默认自动挑中文")
    args = p.parse_args()

    # 先确认服务健康
    try:
        with urllib.request.urlopen(f"{args.url.rstrip('/')}/health", timeout=20) as r:
            health = json.loads(r.read().decode("utf-8"))
        print(f"服务状态: {health}")
    except Exception as exc:  # noqa: BLE001
        print(f"连不上 ASR 服务 {args.url}: {exc}")
        print("请先启动: .venv\\Scripts\\python.exe server.py")
        return 1

    voice = args.voice or pick_chinese_voice()
    if not voice:
        print("找不到中文语音，无法生成测试语料")
        return 1
    print(f"TTS 语音: {voice}")
    print()

    rows: list[tuple[str, str, float, float, float]] = []
    print(f"{'原文':<22} {'识别结果':<24} {'时长':>6} {'延迟':>7} {'RTF':>6} {'CER':>6}")
    print("-" * 82)

    for i, text in enumerate(SENTENCES):
        wav_path = OUT_DIR / f"s{i:02d}.wav"
        if not synth(text, wav_path, voice):
            print(f"  {text:<20} 合成失败，跳过")
            continue
        wav_bytes = wav_path.read_bytes()
        dur = wav_duration(wav_bytes)

        t0 = time.perf_counter()
        try:
            res = post_audio(args.url, wav_bytes)
        except Exception as exc:  # noqa: BLE001
            print(f"  {text:<20} 请求失败: {exc}")
            continue
        wall = time.perf_counter() - t0

        hyp = str(res.get("text", ""))
        rtf = float(res.get("rtf", wall / dur if dur else 0))
        e = cer(text, hyp)
        rows.append((text, hyp, dur, wall, rtf))
        print(f"{text:<22} {hyp:<24} {dur:>5.1f}s {wall:>6.2f}s {rtf:>6.3f} {e:>5.1%}")

    if not rows:
        print("\n没有成功的样本")
        return 1

    print()
    print("=" * 82)
    n = len(rows)
    avg_wall = sum(r[3] for r in rows) / n
    avg_rtf = sum(r[4] for r in rows) / n
    avg_cer = sum(cer(r[0], r[1]) for r in rows) / n
    print(f"样本 {n} 句")
    print(f"  平均端到端延迟 : {avg_wall:.2f} s")
    print(f"  平均 RTF       : {avg_rtf:.3f}   (<1 表示比实时快)")
    print(f"  平均字错率 CER : {avg_cer:.1%}")
    print()
    print("判读参考：")
    print("  RTF < 1 说明推理比录音快，可实时使用；越小越好")
    print("  对话场景 CER 低于约 10% 基本可用，这里因为语料是合成音所以会偏低")
    print("  真实识别率请以对着板子说话为准")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
