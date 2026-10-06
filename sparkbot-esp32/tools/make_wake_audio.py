"""用 Windows 中文语音合成唤醒词音频，用于自动化验证唤醒词。

为什么需要它：唤醒词只能靠"说"来验证，但开发和回归测试时没法每次都靠人喊。
这个脚本用本机的中文 TTS（Microsoft Huihui，zh-CN）合成出唤醒词音频，
再交给板子从喇叭放出来 —— 板子自己的麦克风听到后应当触发唤醒。

注意这是**自检回路**（喇叭 → 空间 → 麦克风），能否成功取决于音量与环境噪声；
失败不代表唤醒词坏了，但成功可以确认整条链路（模型加载 + AFE + 检测回调）是通的。
真实使用的最终判据仍然是**你本人在安静环境下喊一声**。

用法::

    # 合成默认唤醒词，输出 WAV
    python tools/make_wake_audio.py

    # 指定文本与输出路径
    python tools/make_wake_audio.py --text "Hi,小星" --out out/wake.wav

    # 列出本机可用语音
    python tools/make_wake_audio.py --list

依赖：Windows PowerShell（System.Speech）。输出统一转成
**16kHz / 16bit / 单声道 WAV** —— 与板子 I2S 采样率一致，避免重采样引入失真。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: PowerShell 脚本：用 System.Speech 合成，直接指定 16kHz 单声道 PCM
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

_PS_LIST = r"""
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.GetInstalledVoices() | ForEach-Object {
    $i = $_.VoiceInfo
    Write-Output ("{0}|{1}|{2}" -f $i.Name, $i.Culture, $i.Gender)
}
$synth.Dispose()
"""


def list_voices() -> list[tuple[str, str, str]]:
    """列出本机可用语音，返回 (名称, 语言, 性别)。"""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_LIST],
        capture_output=True,
        text=True,
        timeout=60,
    )
    voices: list[tuple[str, str, str]] = []
    for line in (out.stdout or "").splitlines():
        parts = line.strip().split("|")
        if len(parts) == 3:
            voices.append((parts[0], parts[1], parts[2]))
    return voices


def synthesize(text: str, out_path: Path, voice: str | None) -> bool:
    """合成文本到 WAV（16kHz/16bit/单声道）。

    实现说明：把 PowerShell 脚本写成**临时 .ps1 文件**再用 ``-File`` 调用，
    而不是 ``-Command`` 拼接参数。用 ``-Command`` 时后面的 ``-Text``
    PowerShell 不会当成参数绑定，参数丢失后脚本仍然"成功"退出，
    结果就是**报告成功但文件根本没生成** —— 很难查的一类静默失败。
    """
    import tempfile

    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as f:
        f.write(_PS_SYNTH)
        script = f.name

    try:
        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                script,
                "-Text",
                text,
                "-OutPath",
                str(out_path),
                "-VoiceName",
                voice or "",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        Path(script).unlink(missing_ok=True)

    if "OK" not in (out.stdout or ""):
        print("合成输出：", (out.stdout or "")[:300])
        print("合成错误：", (out.stderr or "")[:300])
        return False

    # 明确确认文件真的落地了，而不是相信脚本的 "OK"
    if not out_path.is_file() or out_path.stat().st_size < 1000:
        print(f"脚本报成功但文件不存在或过小: {out_path}")
        return False
    return True


def describe_wav(path: Path) -> None:
    """打印 WAV 的关键参数，确认采样率/位深/声道符合板子要求。"""
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        ch = w.getnchannels()
        width = w.getsampwidth()
        frames = w.getnframes()
    print(f"  采样率 {rate} Hz | 声道 {ch} | 位深 {width * 8} bit | 时长 {frames / rate:.2f} 秒")
    if (rate, ch, width) != (16000, 1, 2):
        print("  ⚠ 不是 16kHz/单声道/16bit —— 板子会重采样，效果可能变差")


def main() -> int:
    """命令行入口。"""
    p = argparse.ArgumentParser(description="合成唤醒词音频用于自动化验证")
    p.add_argument("--text", default="Hi,小星", help="要合成的文本")
    p.add_argument("--out", default=str(ROOT / "artifacts" / "wake" / "wake_hixiaoxing.wav"))
    p.add_argument("--voice", default=None, help="语音名（默认自动挑中文）")
    p.add_argument("--list", action="store_true", help="只列出可用语音")
    args = p.parse_args()

    voices = list_voices()
    if args.list or not voices:
        print("本机可用语音：")
        for name, culture, gender in voices:
            print(f"  {name} | {culture} | {gender}")
        if not voices:
            print("  （没有可用语音，无法合成）")
        return 0 if args.list else 1

    # 没指定就用第一个中文语音；唤醒词是中文，英文语音读不对
    voice = args.voice
    if not voice:
        zh = [v for v in voices if v[1].lower().startswith("zh")]
        if not zh:
            print("没有中文语音，无法合成中文唤醒词。可用：")
            for v in voices:
                print(f"  {v[0]} | {v[1]}")
            return 1
        voice = zh[0][0]

    out_path = Path(args.out)
    print(f"语音: {voice}")
    print(f"文本: {args.text}")
    if not synthesize(args.text, out_path, voice):
        return 1
    print(f"已生成: {out_path}")
    describe_wav(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
