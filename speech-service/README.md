# 本地语音服务（SenseVoice ASR）

CPU 上本地部署的语音识别服务，提供 OpenAI 兼容接口，供 `D:\dsh\sparkbot`
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
| TTS（edge-tts） | 8761 | 后台最小化窗口，约 2 秒就绪 |
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

磁盘占用：venv 约 1GB + 模型 896MB + pip 缓存（在 `D:\dsh\.tmp`）。

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

## 尚未部署：TTS

主项目还需要 `/v1/audio/speech`（文本转语音）才能真正"对话"。
目前 `speech.tts_provider` 仍是 `mock`，板子只会发出高低音。

**CosyVoice2 的情况与本服务不同**：

- 官方**没有** OpenAI 兼容服务，需要自己写 `/v1/audio/speech` 端点
  （约 80 行，参考本项目 `server.py` 的结构）
- 模型约 1.5GB，CPU 上合成一句话预计 **3~15 秒**，对话会有明显停顿
- 输出采样率约 22.05kHz，而板子要 16kHz —— 好消息是固件里
  `bot_audio_play()` **已实现自动线性重采样**，不需要额外处理

CPU 场景下更轻的替代：`edge-tts`（调微软免费接口，几乎零延迟、
音质好），代价是走网络、非完全离线。

---

## 文件

| 文件 | 说明 |
|---|---|
| `server.py` | ASR 服务（FastAPI，OpenAI 兼容） |
| `start_asr.bat` | 启动脚本（含全部环境变量处理） |
| `benchmark.py` | 实测脚本：准确率（CER）+ 延迟（RTF） |
| `bench/` | 基准测试生成的音频 |
| `modelscope-cache/` | 模型缓存（896MB，可删，删后重新下载） |
| `home/` | 为 ModelScope 准备的可写"家目录"（很小） |
