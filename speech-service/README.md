# 本地语音服务（SenseVoice ASR + MOSS-TTS-Nano TTS）

CPU 上本地部署的语音服务（识别 + 合成），提供 OpenAI 兼容接口，供 `D:\dsh\sparkbot`
主项目调用。**不需要 GPU、不需要云 API、不产生费用。**

---

## 快速开始

### 一键启动整套（推荐）

在**主项目**目录下：

```bat
cd D:\dsh\sparkbot
start_stack.bat
```

它会拉起三个进程：

| 服务 | 端口 | 说明 |
|---|---|---|
| ASR（SenseVoice） | 8760 | 后台最小化窗口，模型加载约 12~15 秒 |
| TTS（MOSS-TTS-Nano） | 8761 | 后台最小化窗口，离线 CPU，模型加载约 10 秒 |
| 主服务 SparkBot | 8765 | **在当前窗口前台运行**，日志实时可见 |

参数：

```bat
start_stack.bat 8800              :: 自定义主服务端口
start_stack.bat 8765 noasr        :: 只跳过 ASR
start_stack.bat 8765 notts        :: 只跳过 TTS
start_stack.bat 8765 noasr notts  :: 只起主服务（用云端或真实设备时）
```

停止：

```bat
stop_stack.bat
```

> **注意**：主服务在前台运行，所以**关闭那个窗口或按 Ctrl+C 只会停掉主服务**，
> 后台的 ASR/TTS 需要跑 `stop_stack.bat` 才会一起停。

### 单独启动某个服务

```bat
cd D:\dsh\sparkbot\speech-service
start_asr.bat            :: ASR，默认 8760
start_tts.bat            :: TTS，默认 8761
start_tts.bat 8761 zh-CN-YunxiNeural   :: 换男声
```

健康检查：

```bash
curl http://127.0.0.1:8760/health
curl http://127.0.0.1:8761/health
curl http://127.0.0.1:8765/health
```

---

## 实测性能（本机，纯 CPU）

| 指标 | 实测值 |
|---|---|
| 模型加载 | 12.8 ~ 14.5 秒 |
| 单次识别延迟（2.16s 音频） | **268 ms** |
| **RTF** | **0.124**（比实时快约 8 倍） |
| 模型体积 | 896 MB |
| 端到端一轮语音对话 | 约 14 秒（其中约 10 秒是等用户说话的采集窗口） |

**RTF 0.124 意味着**：10 秒语音只需约 1.2 秒识别 —— CPU 上完全实时可用，
不需要 GPU。

---

## 接进主项目

主项目把语音设计成调用**云端 OpenAI 兼容接口**，所以本地部署只需改
`base_url`，**架构零改动**：

| 字段 | 值 |
|---|---|
| `speech.asr_provider` | `openai` ← 表示"OpenAI 兼容协议"，不是"必须用 OpenAI" |
| `speech.asr_base_url` | `http://127.0.0.1:8760/v1` |
| `speech.asr_model` | `iic/SenseVoiceSmall` |
| `speech.asr_api_key` | `local`（本地服务不校验，填任意非空值） |

已配置好并验证。改配置用控制台设置页，或：

```bash
curl -X POST http://127.0.0.1:8765/api/config \
  -H "Content-Type: application/json" \
  -d '{"values":{"speech.asr_provider":"openai","speech.asr_base_url":"http://127.0.0.1:8760/v1","speech.asr_model":"iic/SenseVoiceSmall","speech.asr_api_key":"local"}}'
```

> 注意请求体要包在 `values` 里，不是平铺的字段。

---

## 为什么需要这个服务（而不是直接 import）

主项目被设计成调 HTTP 接口。两条路：

| | 起兼容服务（本方案） | 主项目内 import |
|---|---|---|
| 主项目改动 | 只改 base_url | 要写新 provider |
| 依赖 | 隔离在本 venv | torch 等塞进主项目 |
| 模型加载 | 服务常驻，加载一次 | 每次启动都加载 |
| Python 版本 | 独立，互不影响 | 与主项目绑定 |

---

## 踩过的坑（都在脚本里处理了）

### 0. `.bat` 文件必须是纯 ASCII（我在这里栽过一次）

**cmd.exe 按系统 ANSI 代码页（本机是 GBK）解析 `.bat` 文件。**
UTF-8 编码的中文注释会被读成乱码字节，进而**被当成命令执行**：

```
'式都很隐蔽?REM' 不是内部或外部命令
'""' 不是内部或外部命令
```

表现是脚本"能运行但什么都不做"，非常难查。

→ **`.bat` 一律只写英文注释**，中文文档放 README。可以用下面这条检查：

```powershell
$b = [System.IO.File]::ReadAllBytes('start_asr.bat')
($b | Where-Object { $_ -gt 127 }).Count   # 必须是 0
```

### 1. ModelScope 要在家目录建缓存，而家目录不可写

```
[E1022] Failed to create SDK directories:
  [WinError 5] 拒绝访问: 'C:\Users\Administrator\.modelscope'
```

表现为"模型下载失败 / 模型未注册"。

→ **必须把 `HOME` / `USERPROFILE` 指到可写目录**。只设 `MODELSCOPE_CACHE`
不够 —— 它还要在 `~/.modelscope` 下建 credentials 目录。

### 2. 覆盖 HOME 会破坏 Windows TTS（System.Speech）

同一个环境变量坑了另一头：`HOME`/`USERPROFILE` 被改后，
PowerShell 的 `System.Speech` 找不到已安装语音：

```
SelectVoice 失败: No voice installed on the system or none available
                 with the current security setting
```

→ **跑 Windows TTS 的脚本（如 `benchmark.py`、主项目里的音频生成工具）
不要在设了 HOME 覆盖的会话里运行。** 两者环境要分开。

### 3. 缺 fbank 后端

```
ImportError: torchaudio is not installed and neither is the
kaldi-native-fbank fallback backend.
FunASR needs one fbank backend for feature extraction.
```

→ 装更轻的 `kaldi-native-fbank`（308KB），不必装完整的 torchaudio：

```bash
.venv\Scripts\pip install kaldi-native-fbank
```

### 4. `torch` 不会自动装上

`pip install funasr` **不包含** torch。要单独装，且建议用 CPU 索引
避免拉 CUDA 版本（省几 GB）：

```bash
pip install --index-url https://download.pytorch.org/whl/cpu torch
```

### 5. pip 缓存目录权限被拒

```
WARNING: Building wheel for jieba failed: [WinError 5]
  'c:\users\administrator\appdata\local\pip\cache\wheels\8d'
```

→ 把 `PIP_CACHE_DIR` / `TMP` / `TEMP` 指到可写盘再装。

### 6. 端口冲突

ModelScope 加载模型要一分多钟，期间端口被占的话**要等它加载完才报 bind 失败**，
很难当场发现。

→ 启动前先确认端口空闲。本服务默认用 **8760**，避开已被占用的 8000。

---

## 环境与依赖

Python **3.13.15**（已验证可用；`torch` / `onnxruntime` / `numpy`
都有 cp313 wheel，其余是纯 Python 包）。

| 依赖 | 版本 |
|---|---|
| funasr | 1.4.16 |
| torch | 2.14.1+cpu |
| onnxruntime | 1.30.0 |
| kaldi-native-fbank | 1.22.3 |
| fastapi / uvicorn | 0.142.2 / 0.54.0 |
| modelscope | 1.40.1 |
| kokoro-onnx | 0.6.1（TTS，纯 ONNX） |
| misaki-fork[zh] | 0.9.6（中文 G2P，需 `--ignore-requires-python`） |
| sherpa-onnx | 1.13.8（TTS，默认引擎，原生 16kHz） |
| edge-tts | 7.2.8（仅作兜底） |

磁盘占用：venv 约 1GB + ASR 模型 896MB + sherpa 模型 131MB +
Kokoro 模型约 450MB + pip 缓存（在 `D:\dsh\.tmp`）。

---

## 接口说明

### `POST /v1/audio/transcriptions`

与 OpenAI 一致，主项目直接调用。

请求（multipart）：
- `file` = WAV（16kHz / 16bit / 单声道）
- `model` = 模型名（服务只加载一个模型，此字段被接受但忽略）

响应：

```json
{
  "text": "识别出的文字",
  "model": "iic/SenseVoiceSmall",
  "audio_seconds": 2.16,
  "infer_ms": 267.5,
  "rtf": 0.1238
}
```

后三个字段是额外的诊断信息 —— OpenAI 客户端会忽略未知字段，不影响兼容性。

### 其他端点

- `GET /health` —— 就绪探针，返回 `status` / `model` / `load_seconds`
- `GET /v1/models` —— 列出模型，可用于验证服务真的加载好了

---

## TTS：MOSS-TTS-Nano（默认）

`moss_tts_server.py` 是现在的默认 TTS。它和 `tts_server.py` 提供**同一套**
OpenAI 兼容接口，主项目只改 `speech.tts_model` / `speech.tts_base_url` 就能换引擎，
不用动一行代码。

用的是 [MOSS-TTS-Nano](https://huggingface.co/OpenMOSS-Team/MOSS-TTS-Nano-100M)
（MOSI.AI + 复旦 OpenMOSS 开源）：

| 项 | 值 |
|---|---|
| 参数 | 0.1B（100M） |
| 硬件 | 纯 CPU，不需要 GPU |
| 网络 | 完全离线 |
| 输出 | 原生 48kHz 立体声（服务端重采样到 16kHz 单声道给板子） |
| 音色 | 声音克隆：给一段参考音频就能定音色 |
| 许可 | Apache-2.0 |

### 为什么换掉 sherpa / edge-tts

* edge-tts 是**在线**服务（微软免费接口），实测会 502/503，机器人播报直接失败；
* sherpa 的中文 VITS 只有 131MB，音色「能听」但明显发闷；
* MOSS-TTS-Nano 只有 0.1B 参数，CPU 上跑得动，音质明显更好，而且完全离线。

### 环境与依赖（单独一个 venv，重要）

MOSS-TTS-Nano 的官方代码 `import torchaudio`，而 **torchaudio 在 2.9 之后停止发版**
（2.8.0 是最后一个配 torch 2.8.x 的版本）。ASR 的 `.venv` 里是 torch 2.14.1，
根本没有对应的 torchaudio —— 两者无法共存，所以 TTS 单独用 `.venv-moss`，
版本组合照抄官方 Space 验证过的那套：

| 依赖 | 版本 |
|---|---|
| torch | 2.8.0+cpu |
| torchaudio | 2.8.0+cpu |
| transformers | 4.57.1 |
| sentencepiece / soundfile | 最新 |
| fastapi / uvicorn / pydantic | 同主服务 |

好处是**完全不碰** `.venv`，ASR 那边照旧工作。

### 安装与模型下载

```powershell
cd D:\dsh\sparkbot\speech-service

# 1) 建独立 venv
& 'C:\Program Files\Python313\python.exe' -m venv .venv-moss

# 2) 依赖。国内走清华镜像；torch / torchaudio 走 PyTorch 官方 CPU 源。
#    注意 TMP/TEMP 必须指向可写盘，否则 pip 会报 No usable temporary directory
$env:TMP='D:\dsh\.tmp'; $env:TEMP='D:\dsh\.tmp'
.venv-moss\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple `
    numpy==2.2.6 sentencepiece soundfile "fastapi>=0.115" "uvicorn[standard]>=0.34" `
    "pydantic>=2.10" "huggingface_hub>=0.34" transformers==4.57.1
.venv-moss\Scripts\python.exe -m pip install --index-url https://download.pytorch.org/whl/cpu `
    torch==2.8.0 torchaudio==2.8.0

# 3) 权重（约 310MB）。HuggingFace 直连不通，走 hf-mirror。
$env:HF_ENDPOINT='https://hf-mirror.com'
.venv\Scripts\python.exe -c @"
from huggingface_hub import snapshot_download
root = r'D:\dsh\sparkbot\speech-service\models\moss'
snapshot_download('OpenMOSS-Team/MOSS-TTS-Nano-100M', local_dir=root + r'\tts',
    allow_patterns=['*.json', '*.py', '*.bin', '*.model', '*.txt'])
snapshot_download('OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano', local_dir=root + r'\codec',
    allow_patterns=['*.json', '*.py', '*.safetensors', '*.txt'])
"@
```

实测 `pytorch_model.bin`（223MB）用 huggingface_hub 下到 0 字节就卡住不动，
换成 `curl -L -C -` 直接下 `.../resolve/main/pytorch_model.bin` 才通 —— 卡住时照这个来。

### 参考音色（声音克隆）

模型靠**参考音频**定音色，参考音色放在 `models/moss/voices/`（不入库）。
仓库预置了 4 个，取自官方 Space 的 `assets/audio/`：

| 音色名 | 参考文件 | 说明 |
|---|---|---|
| `Junhao`（默认） | `zh_1.wav` | 中文男声 A |
| `Xiaoyu` | `zh_3.wav` | 中文女声 A |
| `Yuewen` | `zh_4.wav` | 中文女声 B |
| `Lingyu` | `zh_6.wav` | 中文女声 C |

想换音色：把一段 5~15 秒的干净人声 wav 丢进 `models/moss/voices/`，
在 `moss_tts_server.py` 的 `VOICE_PRESETS` 里加一行即可 —— **不需要训练**。
请求里的 `voice` 也可以直接写参考音频的绝对路径。

### 排障开关

| 命令 | 行为 |
|---|---|
| `start_tts.bat` | 默认 `Junhao`，启动即预热模型 |
| `start_tts.bat 8761 Xiaoyu` | 换默认音色 |
| `moss_tts_server.py --no-preload` | 启动不加载模型，首个请求再加载 |
| `moss_tts_server.py --threads 8` | 指定 torch 线程数 |
| `moss_tts_server.py --nq 8` | 少用音频码本：更快、音质略降 |

`GET /health` 回报 `ready` / `error` / `load_seconds` / `voices`；
`POST /v1/audio/speech` 的响应头带 `X-TTS-Engine` / `X-Synth-Ms` / `X-Audio-Seconds`。

### 已知限制

* 模型不支持变速，请求里的 `speed` 会被忽略；
* 没做数字/日期文本正规化（官方用 WeTextProcessing，Windows 上装 pynini 比较费劲）。
  「三点半」这类中文写法没问题，纯阿拉伯数字偶尔读得生硬；
* 服务端**整段合成后一次性下发**，不做真流式 —— 板子实测连续小块喂 I2S 会「呲呲」，
  和提交「播报不再走流式」保持一致。

---

## 备选 TTS：sherpa-onnx / Kokoro / edge-tts

`tts_server.py` 提供 `/v1/audio/speech`，主项目把 `speech.tts_provider`
设成 `openai`、base_url 指向本服务即可。服务内置三个离线/在线引擎，
请求里的 `model` 字段直接选，PC 端改 `speech.tts_model` 就能热切换，
不用重启：

| `model` | 引擎 | 输出采样率 | 音色 |
|---|---|---|---|
| `sherpa`（默认） | sherpa-onnx VITS 中文 | **原生 16kHz** | `sid_0` ~ `sid_186`（187 个说话人） |
| `kokoro` | Kokoro-82M v1.1-zh | 24kHz（服务端重采样到 16k） | `zf_001` 等 103 个 |
| `edge` | 微软 edge-tts（在线） | 24kHz | edge 中文音色名 |

### 为什么默认是 sherpa-onnx

关键在采样率。板子 I2S 固定 16kHz，而固件里的重采样函数
`resample_s16_mono` 是**线性插值**、没有抗混叠滤波 —— 24k→16k 会把
8kHz 以上的成分折叠回来，齿音发毛、听感明显不自然。

sherpa 的中文 VITS 模型**原生输出 16kHz**，与板子完全一致，整条链路
连重采样这一步都不存在，直接绕开了这个问题。

其余实测对比（i5-11400，纯 CPU，同一句话）：

| | sherpa-onnx | Kokoro |
|---|---|---|
| 模型加载 | **0.9s** | 3.5s |
| 合成速度 | **RTF ≈ 0.30** | RTF ≈ 0.40 |
| 输出采样率 | **16kHz（原生）** | 24kHz（需重采样） |
| 音色数量 | **187** | 103 |
| 许可 | Apache-2.0 | Apache-2.0 |

### 为什么换掉 edge-tts

最初用的是 edge-tts（调微软免费接口）。它音质不错、延迟也低，但**是在线服务**，
实测会返回 502/503（`Invalid response status` / `No audio was received`），
机器人播报直接失败。对话机器人不能把"能不能出声"押在不保证可用的免费接口上。

### sherpa 的安装与模型下载

```powershell
# 1) 装 sherpa-onnx（二进制 wheel，国内走清华镜像）
.venv\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple sherpa-onnx

# 2) 下模型（131MB / 17 个文件）。GitHub Releases 国内极慢，走 HF 镜像。
$root = "models\sherpa\vits-zh-hf-fanchen-C"
$base = "https://hf-mirror.com/csukuangfj/vits-zh-hf-fanchen-C/resolve/main"
$files = @("vits-zh-hf-fanchen-C.onnx","lexicon.txt","tokens.txt",
           "date.fst","phone.fst","number.fst","new_heteronym.fst","G_C.json",
           "dict/hmm_model.utf8","dict/idf.utf8","dict/jieba.dict.utf8",
           "dict/stop_words.utf8","dict/user.dict.utf8",
           "dict/pos_dict/char_state_tab.utf8","dict/pos_dict/prob_emit.utf8",
           "dict/pos_dict/prob_start.utf8","dict/pos_dict/prob_trans.utf8")
New-Item -ItemType Directory -Path $root -Force | Out-Null
foreach ($f in $files) {
    $dst = Join-Path $root $f
    New-Item -ItemType Directory -Path (Split-Path $dst -Parent) -Force | Out-Null
    curl.exe -L -s -o $dst "$base/$f"
}
```

声线直接用**说话人编号**：请求里的 `voice` 写 `sid_0` ~ `sid_186`，
不在这个范围就回落到默认说话人（`DEFAULT_SHERPA_SID`）。想挑音色就依次
改 `speech.tts_voice` 试听。

### 备选引擎：Kokoro（中文音色更多）

| 项 | 值 |
|---|---|
| 许可 | MIT |
| 体积 | 82M 参数，fp32 ONNX 324MB |
| 硬件 | 纯 CPU，不需要 GPU |
| 延迟 | 本机 RTF ≈ 0.37~0.56（比实时快 2~3 倍） |
| 中文 | 有专门的中文模型 Kokoro-82M-v1.1-zh 与成体系的中文声线 |

实测（i5-11400，纯 CPU）：

```
16 字 -> 3.25s 音频, 总计 1322ms | RTF 0.407
37 字 -> 7.01s 音频, 总计 2609ms | RTF 0.372
```

### 必须用 fp32 模型（重要）

同一份模型的 **int8 量化版在本机 RTF ≈ 3.8，比实时慢 4 倍**，
反而比 fp32 慢约 9 倍。x86 上 ORT 的量化卷积/矩阵乘内核在这个小模型上
不占优，还要额外承担量化/反量化开销。所以固定加载
`kokoro-v1.1-zh.fp32.onnx`，**不要换成 int8**。

（int8 实测：4/6/12 线程分别 RTF 4.05 / 3.98 / 3.89 —— 瓶颈不是线程调度。）

### Kokoro 的安装与模型下载

模型文件较大（约 450MB），**不入库**（`.gitignore` 里已排除 `models/`）。
换台机器要重新准备一次：

```powershell
# 1) 装依赖。misaki 声明 Python<3.13，但它是纯 Python 包，
#    实测在 3.13 上工作正常，用 --ignore-requires-python 绕过即可。
#    国内必须加镜像，否则 PyPI 慢到不可用（实测 10 KB/s）。
$mirror = "https://pypi.tuna.tsinghua.edu.cn/simple"
.venv\Scripts\python.exe -m pip install -i $mirror kokoro-onnx
.venv\Scripts\python.exe -m pip install -i $mirror --ignore-requires-python "misaki-fork[zh]"

# 2) 下模型。HuggingFace 直连不通，用 hf-mirror 镜像。
$dir  = "models\kokoro"
$base = "https://hf-mirror.com/onnx-community/Kokoro-82M-v1.1-zh-ONNX/resolve/main"
New-Item -ItemType Directory -Path "$dir\voices" -Force | Out-Null

#    fp32 模型（324MB）—— 必须用这个，别用 int8
curl.exe -L -o "$dir\kokoro-v1.1-zh.fp32.onnx" "$base/onnx/model.onnx"

#    词表（misaki 的音素 -> ID 映射）
curl.exe -L -o "$dir\config.json" `
    "https://hf-mirror.com/hexgrad/Kokoro-82M-v1.1-zh/raw/main/config.json"

#    声线：按需挑 zf_*.bin（女）/ zm_*.bin（男），每个 510KB
foreach ($n in "zf_001","zf_002","zf_003","zm_009","zm_010","zm_011") {
    curl.exe -L -o "$dir\voices\$n.bin" "$base/voices/$n.bin"
}

# 3) 散装声线打包成 kokoro-onnx 要的 npz
#    （每个文件是 510x256 的 float32，np.load 读的就是这个 npz）
.venv\Scripts\python.exe -c @"
import numpy as np, pathlib
d  = pathlib.Path(r'models/kokoro')
vs = {f.stem: np.fromfile(f, np.float32).reshape(-1, 1, 256)
      for f in sorted((d / 'voices').glob('*.bin'))}
np.savez(d / 'voices-zh.npz', **vs)
print('voices:', sorted(vs))
"@
```

### 声线映射

PC 端 `.env` 里写的是 edge 风格的名字，服务内部做一次映射：

| 请求里的 voice | 实际用的 Kokoro 声线 |
|---|---|
| `zh-CN-XiaoxiaoNeural` | `zf_001` |
| `zh-CN-XiaoyiNeural` | `zf_002` |
| `zh-CN-YunxiNeural` | `zm_009` |
| `zh-CN-YunjianNeural` | `zm_010` |
| `zh-CN-YunyangNeural` | `zm_011` |
| `zh-CN-YunxiaNeural` | `zm_012` |

直接传 Kokoro 声线名（`zf_001` 等）也可以。想换音色不必动 PC 端配置，
用 `--voice zf_003` 启动本服务即可。

### 排障开关

| 命令 | 行为 |
|---|---|
| `start_tts.bat` | 默认 `auto`：Kokoro 优先，失败回落 edge-tts |
| `tts_server.py --engine kokoro` | 只用 Kokoro（离线） |
| `tts_server.py --engine edge` | 只用 edge-tts（在线，用来对比排障） |

`GET /health` 会回报 `kokoro_ready` / `kokoro_error` / `voices`；
`POST /v1/audio/speech` 的响应头带 `X-TTS-Engine`，标明这次实际用了哪个引擎。

### 已知限制

misaki 的中文 G2P 在没有英文前端时会丢掉英文单词（启动时打印
`en_callable is None, so English may be removed`）。当前机器人回复以中文为主，
暂不处理；若以后回复里常夹英文，需要给 `ZHG2P` 传一个英文 `en_callable`。

---

## 文件

| 文件 | 说明 |
|---|---|
| `server.py` | ASR 服务（FastAPI，OpenAI 兼容） |
| `start_asr.bat` | 启动脚本（含全部环境变量处理） |
| `moss_tts_server.py` | **默认** TTS 服务（MOSS-TTS-Nano，离线 CPU） |
| `tts_server.py` | 备选 TTS 服务（Kokoro 离线 + edge-tts 兜底） |
| `start_tts.bat` | TTS 启动脚本（拉起 `.venv-moss` 里的 MOSS-TTS-Nano） |
| `models/moss/` | MOSS-TTS-Nano 权重 + 参考音色（约 310MB，可删，见上文重新下载） |
| `models/kokoro/` | Kokoro 模型与声线（约 450MB，可删，见上文重新下载） |
| `models/sherpa/` | sherpa-onnx 模型（131MB，可删，见上文重新下载） |
| `benchmark.py` | 实测脚本：准确率（CER）+ 延迟（RTF） |
| `bench/` | 基准测试生成的音频 |
| `modelscope-cache/` | 模型缓存（896MB，可删，删后重新下载） |
| `home/` | 为 ModelScope 准备的可写"家目录"（很小） |
