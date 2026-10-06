# SparkBot

面向 **ESP32-S3 移动机器人** 的 PC 端 Agent 框架。
机器人只有底盘轮子：用**摄像头**看、用**麦克风**听、用**喇叭**说、用 **LCD** 表达情绪。

ESP32-S3 负责实时控制（电机、I2S 音频、摄像头、屏幕、本地唤醒词），
PC 负责视觉理解、语音识别与合成、对话决策和工具编排。
**换模型、改人格、加工具都不需要重新烧录固件。**

---

## 30 秒跑起来

不需要硬件，也不需要任何 API key —— 内置离线假模型。

### Windows：双击 `start.bat`（第一次用）

自动检查并安装依赖、启动服务、打开浏览器。**关掉那个窗口 = 停止服务。**

### 日常启停：`start_all.bat` / `stop_all.bat`

```bat
start_all.bat                :: 起服务 + 模拟设备，各占一个独立窗口
start_all.bat 8765 8         :: 每 8 秒模拟一次唤醒词（语音闭环会自动转）
start_all.bat 8800           :: 换端口
start_all.bat 8765 0 nodev   :: 只起服务，不模拟设备（接真板子时用）

stop_all.bat                 :: 一键停止两个进程，并报告端口是否释放
```

两个进程会**脱离当前批处理窗口独立运行**，启动完可以关掉那个窗口。
日志写到 `logs\server.log` 与 `logs\device.log`。

### 或者手动两个终端

```bash
# 终端 1：启动 PC 端服务（必须一直开着！）
python run.py

# 终端 2：启动模拟 ESP32-S3（实现完整协议行为）
python -m sparkbot.mock_device --trigger 8
```

然后打开 **http://127.0.0.1:8765/** —— 控制台有「对话」和「设置」两个标签页：

* **对话**：在输入框里问「你前面有什么东西？」，你会看到模型调用 `look_around`
  抓帧、并把结果说出来；右侧有 12 个表情按钮、实时状态和事件流；
* **设置**：**在这里就能查看和修改 LLM 模型**（见下一节）。

`--trigger 8` 让模拟设备每 8 秒模拟一次唤醒词，于是**整条语音闭环**
（唤醒 → 采集 → 识别 → 对话 → 播报）会自己转起来。

> ### ⚠️ 服务是前台进程，必须一直开着
>
> 页面打不开的**头号原因**就是服务没在跑。`python run.py` 在前台运行——
> 那个窗口必须留着，关掉窗口或按 Ctrl+C 就等于停服务。
> **重启电脑后也不会自动恢复**，需要重新启动。

---

## 页面打不开？先跑自检

```bash
python check.py
```

它会逐项检查并把结论说清楚：Python 版本、项目文件、依赖是否装齐、
端口是否被占用、应用能否装配、当前用的是哪个模型。

几条最常见的结论：

| 自检输出 | 含义 | 怎么办 |
|---|---|---|
| `端口已被占用` | **服务其实已经在跑了**（最常见） | 直接开 http://127.0.0.1:8765/ ；要重开先跑 `stop_all.bat` |
| `缺少依赖` | `.vendor` 没装或不全 | `python -m pip install --target .vendor -r requirements.txt` |
| `Python 版本过低` | 低于 3.11 | 装 3.11+ |
| 一切正常但页面还是打不开 | 浏览器/代理问题 | 试 `curl http://127.0.0.1:8765/health`；关掉代理或换浏览器 |

如果端口被别的程序占了，换个端口启动：

```bash
python run.py --port 8800
```

服务启动时会**自己检查端口**，被占用会直接告诉你原因和替代命令，
而不是丢一串 uvicorn 的 `[Errno 10048]` 报错。

---

## Web 控制台

服务启动后访问 **http://127.0.0.1:8765/**。

### 对话标签页

| 区域 | 作用 |
|---|---|
| 对话 | 发文字消息走完整 Agent 流程，工具调用会逐条列出（名称、参数、耗时、成败） |
| 表情 | 12 个按钮，点一下直接切屏幕表情 |
| 状态 | 实时 JSON：provider、工具数、在线设备、遥测 |
| 事件流 | 通过 WebSocket 推送的总线事件（设备上下线、工具调用、语音识别…） |
| 顶部按钮 | **急停**（绕过模型直接刹停）、清空对话历史 |

### 设置标签页 —— 查看与配置 LLM

这就是你要的「Web 上配模型」。打开「设置」即可看到当前生效的配置，并在页面上直接改：

| 分组 | 可改项 |
|---|---|
| 大模型 | provider、模型名、base_url、api_key、temperature、max_tokens、工具轮次上限 |
| 视觉理解 | 是否启用、provider、模型名、base_url、api_key |
| 语音 | ASR provider/key、TTS provider/key/发音人 |
| 安全与行为 | 限速线/角速度、单次最长运动、说话时换表情、历史条数 |
| 人格 | 直接编辑 system prompt，改完立刻换性格 |

三个按钮：

* **保存并生效** —— 立即重建 provider（**不用重启服务**），并写入 `.env` 以便重启后仍在；
* **测试连接** —— 用当前配置向 LLM 发一句「请只回复两个字：收到」，回报延迟与用量。
  用来确认 key / base_url / 模型名三者是否配对，成本极低；
* **重新载入** —— 丢弃页面上的改动，重新从服务端读。

右侧「当前生效」实时显示服务**真正在用**的 provider 与模型，可以和表单对照，
避免出现「填了但没生效」。

#### 安全约定（重要）

* **API Key 不会明文回传前端。** 页面只显示「已设置 ✓」；
* 表单里 key **留空 = 保持不变**，所以查看配置不会意外清空 key；
  真要清空请用 API 传 `null`；
* key 会以明文写入 `.env`（这是它必须能被 pydantic 读取的代价），
  所以 **`.env` 已加入 `.gitignore`，不要提交**；
* 只有 `config.EDITABLE_FIELDS` 里登记过的字段能改。监听端口、CORS
  这类启动期设置会被明确拒绝（改了也不会生效，拒绝比静默忽略更清楚）。

#### 用 API 配置（等价于界面的操作）

```bash
# 查看（密钥已打码）
curl http://127.0.0.1:8765/api/config

# 改成 DeepSeek
curl -X POST http://127.0.0.1:8765/api/config \
  -H "Content-Type: application/json" \
  -d '{"values":{"llm.provider":"deepseek",
                 "llm.model":"deepseek-chat",
                 "llm.api_key":"sk-xxxx"}}'

# 测试连通性
curl -X POST http://127.0.0.1:8765/api/config/test
```

> 换了 provider 之后，控制台「对话」页直接就能用新模型——不需要重启。
> 注意 DeepSeek 的对话模型不支持图像输入，「看图」能力需要单独在
> **视觉理解** 分组里配一个多模态模型。

---

## 其它状态接口

| 接口 | 用途 |
|---|---|
| `GET /api/status` | provider、模型、工具数、在线设备、语音闭环状态、运行时长 |
| `GET /api/devices` | 每台设备的完整信息：能力、遥测、运动状态、缓存帧数 |
| `GET /api/tools` | 已注册工具清单（含参数 JSON Schema） |
| `GET /api/events/recent` | 最近 50 条总线事件 |
| `WS /api/events` | 事件流（控制台右侧「事件流」用的就是它） |
| `GET /health` | 健康检查，供监控探针使用 |
| `GET /docs` | FastAPI 自动生成的交互式 API 文档 |

所有接口都是只读的除了 `/api/chat`、`/api/voice`、`/api/stop`、
`/api/action`、`/api/face`、`/api/agent/reset`、`/api/config*`。

---

## 怎么测试

### 一、语音对话（最能体现「机器人」的功能）

```bash
python tests/voice_test.py                # 完整一轮：说话 → 识别 → 决策 → 板子喇叭回答
python tests/voice_test.py --rounds 3     # 连说 3 轮
python tests/voice_test.py --loopback     # 不需要说话：回灌自检喇叭与麦克风
python tests/voice_test.py --capture-only # 只验证采集
python tests/voice_test.py --asr-only     # 只验证识别
```

也可以直接在控制台点 **🎤 语音对话** 按钮（等价于对着板子说唤醒词）。

**语音对话是闭环，任何一环坏了表现都一样（"机器人不理我"）。**
所以这个工具**逐环验证并指出是哪一环断了**：

```
板子麦克风采集 → ASR 识别 → LLM 决策（可能调工具） → TTS 合成 → 板子喇叭播放
```

实测输出（真实板子）：

```
[通过] 设备在线 — 小星 / ESP32-S3 / fw 1.0.0
[通过] 有麦克风 / 有喇叭
[通过] start_listen — 采集已开始
[通过] 板子未重启 — uptime 持续增长
[通过] 识别到语音 — '（离线识别：这里是一句话）'
[通过] 生成了回复 — 我听到你说「…」。我是一台会看、会走、会做表情的小机器人。
[通过] 整轮耗时 — 服务端 9296 ms，端到端 9800 ms
```

> **mock 模式的边界**：默认配的是 `mock` ASR/TTS。
> mock ASR 只返回占位文本，**无法验证识别准确率**；
> mock TTS 用高低音代替语音，**听不到人话**。
> 这不是 bug —— 它验证的是「采集→上行→识别→决策→合成→播放」这条链路是通的。
>
> 要真正测「说话 → 听懂 → 回答」，在控制台「设置」页填：
> `SPARKBOT_LLM_*`（对话）、`SPARKBOT_SPEECH_ASR_*`（识别）、
> `SPARKBOT_SPEECH_TTS_*`（合成）。工具会自动检测并提示当前模式。

**手动触发语音对话**（板子没做本地唤醒词时用）：

```bash
curl -X POST http://127.0.0.1:8765/api/voice/trigger \
  -H "Content-Type: application/json" -d '{"wait": true}'
```

返回 `{"heard": "识别到的文本", "replied": "回复内容", "elapsed_ms": ...}`，
便于脚本判断整轮结果。

### 二、硬件逐项测试（对着真板子）

```bash
python tests/hw_test.py                # 全量：状态→屏幕→喇叭→麦克风→摄像头→反向测试
python tests/hw_test.py --display      # 只测屏幕（表情/文字/背光）
python tests/hw_test.py --speaker      # 只测喇叭（音阶）
python tests/hw_test.py --mic          # 只测麦克风（采集会话与自动结束）
python tests/hw_test.py --camera       # 只测摄像头
python tests/hw_test.py --negative     # 只做反向测试
python tests/hw_test.py --status       # 只查设备状态
python tests/hw_test.py --tts          # 额外用 PC 端 TTS 说一句话
```

**测试前**：PC 端 `python run.py` 在跑，板子上电并显示 `LINK OK`。

每项都有客观判定依据，不是"命令返回 200 就算过"：

| 测试 | 判定依据 | 需要你做什么 |
|---|---|---|
| 状态 | 设备在线、能力列表、遥测在更新、通信新鲜度 | 无 |
| 屏幕 | 7 种表情依次下发、文字、背光**遥测回读核对** | 盯着屏幕确认 |
| 喇叭 | 4 个递升音阶、边界频率、音量**遥测回读核对** | 听声音 |
| 麦克风 | 采集会话起来、自动结束、**uptime 持续增长证明没重启** | 说句话/拍手 |
| 摄像头 | 抓帧成功、**帧真的到了 PC**（`frames_buffered`） | 无 |
| 反向测试 | 对**没有**的能力发命令必须收到明确错误 | 无 |

反向测试最值得留意：它验证「能力声明」与「实际实现」一致。
把某个外设在 menuconfig 里关掉后，对应命令必须报错而不是静默成功。

### 三、自动化测试（不需要硬件）

```bash
python tests/test_end_to_end.py      # 端到端：真实 HTTP/WS + 模拟设备（71 项断言）
python tests/test_mock_provider.py   # 假模型规则与 Agent 工具链（33 项断言）
python tests/test_config_api.py      # 配置 API、.env 落盘、端口冲突（52 项断言）
python tests/smoke.py                # 对正在运行的服务做全链路自检
```

### 四、看串口日志（排查固件问题必用）

**推荐：Web 控制台的「串口日志」标签页**（见下节），不用命令行、能一边看日志
一边操作设备。

命令行方式（命令行工具仍然保留）：

```bat
C:\Espressif\python_env\idf5.5_py3.13_env\Scripts\python.exe ^
    sparkbot-esp32\tools\serial_log.py COM15 20
```

> **别用 PowerShell 的 `SerialPort` 读**：它打开端口时会翻转 DTR/RTS，
> 而这两根线在 ESP32-S3 的原生 USB 上控制复位与下载模式，
> 结果就是「一读日志板子就重启」，还会把正在做的命令测试一起搞乱。
> 上面的脚本用 pyserial 且显式 `dtr=False, rts=False`。

---

## Web 查看串口日志

控制台新增 **「串口日志」** 标签页：

```
http://127.0.0.1:8765/  →  串口日志
```

1. 选端口（默认自动选 **COM15**）与波特率（ESP32-S3 用 115200）
2. 点 **「打开串口」**
3. 固件输出实时出现；支持**关键字过滤**、**自动滚动**、**自动换行**、**清空**

> ⚠️ **串口是独占资源。** 本页打开期间，`tools/serial_log.py` 和
> `esp32gw.exe` 都会打不开端口。用完请点 **「关闭串口」** 释放出去。
> 打开失败时页面会直接给原因（通常是"端口被占用"）。

相关接口（也可供脚本调用）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/serial/ports` | 列出可用串口 |
| GET | `/api/serial/status` | 当前状态 |
| GET | `/api/serial/recent?limit=N` | 最近 N 行 |
| POST | `/api/serial/open` | `{"port":"COM15","baudrate":115200}` |
| POST | `/api/serial/close` | 关闭并释放端口 |
| POST | `/api/serial/clear` | 清空缓冲 |
| WS | `/api/serial/stream` | 实时推送（页面用的就是它） |

**实现要点**（详见 `sparkbot/serial_log.py` 的注释）：

- **DTR/RTS 显式拉低**：不开这一步，打开串口的瞬间板子就复位了。
- 串口读取跑**独立线程**；WebSocket 发送走 `call_soon_threadsafe` +
  有界队列，队列满丢最旧的，**绝不反压串口线程**（否则会丢真实日志）。
- **按行切分**，半行留到下一批；结尾无换行的残留半行也会发出，不丢最后一条。
- 服务关闭时自动释放串口。

---

## 设备连不上？先查 PC 的 IP 是否变了

固件里的 PC 地址是**硬编码**的（`CONFIG_SPARKBOT_SERVER_HOST`）。PC 走 DHCP，
路由器重新分配地址后设备就连不上，串口会一直刷：

```
bot_net: 连接 PC: 192.168.0.106:8765/robot
bot_net: 连接失败: 104
```

（`104` = ECONNRESET。）

先看 PC 当前 IP：

```powershell
Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -like '192.168.*' }
```

不一致就改掉并重烧：

```bat
cd D:\dsh\sparkbot-esp32
python tools\set_server_host.py 192.168.0.103     :: 填 PC 当前 IP
build.bat build
build.bat flash COM15
```

> 想一劳永逸：在路由器里给 PC 的 MAC 绑定固定 IP（DHCP 保留）。

---

## 长期记忆

机器人能**跨会话记住关于你的事**——重启服务也不会忘。

```
你: 我叫张伟
它: 记住啦，张伟！以后我就这么叫你～
你: 我对花生过敏
它: 花生过敏我记下了，以后帮你留意吃的！
```

一天后想验证：

```
你: 我叫什么？
它: 你叫张伟呀。
```

### 它记什么、放在哪

存在 `artifacts/memory/facts.jsonl`，一行一条，可直接用文本编辑器查看：

```json
{"id":"9325e3573a89","content":"用户叫张伟。","importance":5,"hits":2,"source":"agent"}
```

| 字段 | 含义 |
|---|---|
| `content` | 事实本身（自包含的一句话） |
| `importance` | 1~5，越高越不容易被淘汰，检索时排序也更靠前 |
| `hits` | 被召回/重申的次数，体现"这件事常被提到" |
| `source` | 谁写进来的：`agent`（模型调工具）/ `auto`（规则抽取）/ `user`（控制台） |

### 两条写入路径

1. **模型主动调用 `remember` 工具** —— 它判断值得记就自己记。
2. **规则自动抽取** —— 兜底，防止模型在闲聊中忽略"顺便记一下"。
   只抽第一人称陈述（「我叫…」「我对…过敏」），疑问句不记，
   否则「你叫什么名字」会被当成事实存下来。

两条路径可能对同一件事各写一次，所以有**跨措辞去重**：
「我叫张伟」和「用户叫张伟。」会被判为同一条（靠包含度而不只是
Jaccard —— 后者对这种长度差异不敏感，实测会漏判）。

### 怎么检索

**没有用向量检索**，理由：桌面机器人只需记住少量事实，引入 embeddings
会带来模型下载、CPU 占用和首字延迟，收益远小于成本。改用：

* 中文**单字 + 二元组**切分（不依赖分词库），英文按单词；
* **两阶段检索**：先按"有词元重合"过滤候选，再按加权覆盖率 +
  重要性 + 频次 + 时间新鲜度排序，最后按相对阈值截断。

单字要保留是关键 —— 只切二元组时「他叫什么名字」与「主人叫张伟」
**没有任何共同词元**，查名字会查不到（实测踩过）。

### 管理接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/memory` | 列出全部记忆 + 容量 + 文件路径 |
| GET | `/api/memory/search?q=咖啡` | 按关键词检索（与 Agent 同一套逻辑） |
| POST | `/api/memory` | 手动添加 `{"content":"...","importance":5}` |
| DELETE | `/api/memory/{id}` | 删除一条 |
| POST | `/api/memory/clear` | 全部清空 |

### 配置

| 字段 | 默认 | 说明 |
|---|---|---|
| `memory.enabled` | `true` | 总开关 |
| `memory.path` | `artifacts/memory/facts.jsonl` | 相对**项目根**（不是当前目录） |
| `memory.capacity` | `500` | 超出后按"重要性 + 频次 - 时间衰减"淘汰 |
| `memory.max_injected` | `5` | 每轮最多注入几条到 system prompt |
| `memory.auto_extract` | `true` | 是否启用规则自动抽取 |

`max_injected` 直接吃 token 且影响模型是否被无关信息带偏，别调太高。

### 隐私

记忆是**明文存在本机**的，用户有权查看与清除。让他忘掉某事时说
「忘掉我喜欢咖啡这件事」即可 —— 模型会调用 `forget` 工具按关键词删除。

---

## 接真板子的验证顺序

建议按这个顺序排查，每步只验证一件事：

1. **只连不控** —— 起服务后给板子上电，看控制台「状态」或
   `GET /api/devices` 是否出现设备。**没出现就先别往下走**，
   问题一定在握手：`hello` 格式、`device.id` 缺失、路径不是 `/robot`、
   或者板子连不上 PC 的 IP。服务端日志会打印具体原因。
2. **看遥测** —— 设备上线后等一个心跳周期（默认 10 秒），
   `telemetry` 是否上报。
3. **只测显示** —— `python tests/hw_test.py --display`。
4. **只测喇叭** —— `--speaker`。
5. **只测麦克风** —— `--mic`（这项最容易暴露固件问题）。
6. **只测抓帧** —— `--camera`。
7. **跑反向测试** —— `--negative`，确认能力声明与实际一致。
8. **最后才接真实模型** —— 用控制台「设置」页填 key，点「测试连接」。

### 四、协议一致性检查

如果板子行为和协议文档对不上，对照 `docs/protocol.md` 第 9 节的
**固件实现检查清单**逐条核对。最常见的三个坑：

* 每个音频采集会话**只能发一个** `end` 标记（多发一个会让下一次采集拿到空音频）；
* `drive` 必须到 `duration_ms` 自动停，不能等 PC 再发 `stop`；
* 未知 action 要回 `result{ok:false, error:{code:"unsupported_action"}}`，
  而不是静默丢弃（静默丢弃会表现为「PC 侧卡到超时」）。

---

## 安装

```bash
# 依赖装到工作区内，不污染系统 Python
python -m pip install --target .vendor -r requirements.txt
```

`sparkbot/paths.py` 会在启动时把 `.vendor` 加进 `sys.path`，
所以不需要 `pip install -e .` 或设置 `PYTHONPATH`。

**要求**：Python 3.11+。`Pillow` 可选（模拟摄像头出图更好看、
真实图像会缩图省 token）。

---

## 接入真实模型

有两条路：**改配置就行**（推荐，不用重启），或者用环境变量。

### 方式一：Web 控制台「设置」页（推荐）

起服务 → 打开 http://127.0.0.1:8765/ → 「设置」标签页 → 填 provider / 模型名 /
API Key → 点 **保存并生效** → 点 **测试连接** 确认通了 → 切回「对话」直接用。

配置会自动写入 `.env`，重启服务后依然有效。

### 方式二：环境变量或 `.env`

复制 `.env.example` 为 `.env` 后修改，或直接设环境变量：

```bash
# DeepSeek
export SPARKBOT_LLM_PROVIDER=deepseek
export SPARKBOT_LLM_MODEL=deepseek-chat
export SPARKBOT_LLM_API_KEY=sk-xxxx

# 或者 OpenAI
export SPARKBOT_LLM_PROVIDER=openai
export SPARKBOT_LLM_MODEL=gpt-4o-mini
export SPARKBOT_LLM_API_KEY=sk-xxxx

# 或者任意 OpenAI 兼容网关（vLLM / Ollama / one-api …）
export SPARKBOT_LLM_PROVIDER=openai_compat
export SPARKBOT_LLM_BASE_URL=http://192.168.1.10:8000/v1
export SPARKBOT_LLM_MODEL=qwen2.5-vl-7b
```

或用命令行：`python run.py --provider deepseek --api-key sk-xxxx --model deepseek-chat`

**注意**：DeepSeek 的对话模型不支持图像输入。要真正「看懂画面」，
需要在**视觉理解**那一组单独配一个多模态模型：

```bash
export SPARKBOT_VISION_PROVIDER=openai          # 或 openai_compat 指向本地 VLM
export SPARKBOT_VISION_MODEL=gpt-4o-mini
```

视觉配置留空时会自动复用主 LLM 的 key 与 base_url。

### 语音

```bash
export SPARKBOT_SPEECH_ASR_PROVIDER=openai
export SPARKBOT_SPEECH_ASR_API_KEY=sk-xxxx
export SPARKBOT_SPEECH_TTS_PROVIDER=openai
export SPARKBOT_SPEECH_TTS_API_KEY=sk-xxxx
export SPARKBOT_SPEECH_TTS_VOICE=alloy
```

留空或设为 `mock` 时离线工作：ASR 返回占位文本，TTS 输出提示音 WAV。

全部配置项见 `.env.example`，也可以在控制台「设置」页里改其中大部分。

---

## 对接真实硬件

固件需要实现 **`docs/protocol.md`** 定义的 WebSocket + JSON 协议。
最小实现路径：

1. 连上 `ws://<PC的IP>:8765/robot`；
2. 发一条 `hello`（含 `device.id` 与 `capabilities`）；
3. 循环收指令，每条 `command` 回一条同 `id` 的 `result`；
4. 按 `heartbeat_ms` 周期发 `telemetry`；
5. 实现所需的 action（见协议第 3 节）。

参考实现：`sparkbot/mock_device/device.py` 是一个**完整可运行的协议实现**，
可以直接对照着写 C 代码。它的 `_do_*` 方法与 action 一一对应。

协议第 4 节列出了固件侧**必须**自己做的安全保护（不能只依赖 PC）。

---

## 验证速查

```bash
python tests/test_end_to_end.py      # 端到端：真实 HTTP/WS + 模拟设备（71 项）
python tests/test_mock_provider.py   # 假模型规则与 Agent 工具链（27 项）
python tests/test_config_api.py      # 配置 API 与 .env 落盘（43 项）
python tests/smoke.py                # 对正在运行的服务做全链路自检
```

三个测试套件都不需要硬件、不需要网络、不需要 API key。
完整测试方法与真机联调顺序见上面 **[怎么测试](#怎么测试)** 一节。

---

## 机器人会做什么

13 个开箱可用的工具，模型按需调用（没有对应能力的设备不会看到这些工具）：

| 工具 | 说明 |
|---|---|
| `look_around` | 看眼前的情况，返回识别到的物体与场景描述 |
| `capture_photo` | 抓拍存盘，不做识别 |
| `move_forward` / `move_backward` | 前进 / 后退指定距离 |
| `turn_left` / `turn_right` | 原地转向指定角度 |
| `stop_moving` | 立即停止 |
| `show_emotion` | 12 种表情 |
| `show_text` | 屏幕上显示一行字 |
| `beep` | 提示音 |
| `speak` | 语音合成并说出来 |
| `listen` | 主动打开麦克风听一轮 |
| `get_status` | 电量、运动状态、可用能力 |

加新工具只需在 `sparkbot/brain/tools.py` 里写一个带类型注解和 docstring
的普通函数——JSON Schema 自动生成。扩展指引见
`docs/architecture.md` 第 8 节。

---

## 文档

| 文档 | 内容 |
|---|---|
| [docs/protocol.md](docs/protocol.md) | **设备协议 v1**：信封、动作表、事件、错误码、时序图、固件检查清单 |
| [docs/architecture.md](docs/architecture.md) | 架构分层、完整数据流、六个核心设计决策、并发模型、扩展指引 |

---

## 项目结构

```
sparkbot/
├── app.py          FastAPI 路由 + 内置控制台
├── runtime.py      装配根、生命周期、语音闭环、配置热重载
├── config.py       配置 + 运行期可编辑字段白名单 + .env 回写
├── core/           异常、事件总线、工具注册表
├── llm/            provider 抽象 + OpenAI/DeepSeek + 离线假模型
├── perception/     视觉理解、ASR/TTS
├── device/         协议、WebSocket 网关、能力门面
├── brain/          Agent 循环、工具集、记忆
└── mock_device/    模拟 ESP32-S3

run.py              启动服务（推荐入口）
start.bat           Windows 首次使用：查依赖 + 起服务 + 开浏览器
start_all.bat       Windows 日常启停：服务 + 模拟设备，各自独立窗口
stop_all.bat        Windows 一键停止
start_all.ps1       PowerShell 等价入口（受执行策略限制，见脚本内说明）
check.py            启动前自检：一条命令定位「为什么起不来」
tests/              三个测试套件 + 手动联调脚本
logs/               脚本启动时的运行日志（.gitignore 已忽略）
```

> 脚本文件名以 `.bat` 结尾的都用**纯 ASCII** 编写。这不是偷懒：
> `.bat` 若存成不带 BOM 的 UTF-8，cmd.exe 会按系统 ANSI 代码页解析，
> 中文注释和 `echo` 会被拆成乱码命令直接报错。中文说明放在本文档里。

---

## 安全须知

* **没有鉴权**：设备 WS 与 HTTP API 都无认证，只应部署在可信内网。
* 设备固件必须自己实现速度钳制与超时自动停止，不能只依赖 PC 侧护栏。
* 控制台暴露了急停（`POST /api/stop`），它**绕过模型**直接下发指令。
