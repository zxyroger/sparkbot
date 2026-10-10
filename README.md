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
| `GET /api/frame` | **取一张摄像头画面**（JPEG）。加 `?fresh=true` 现抓一张，浏览器可直接打开看图 |
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
cd D:\dsh\sparkbot\sparkbot-esp32
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

## 人脸识别：设备端推理，Agent 端绑名字

**谁在说话**这件事靠摄像头认脸解决（不用声纹）。分工是刻意切开的：

```
ESP32-S3：抓一帧 → 本地推理（esp-dl：MSR+MNP 检测、MFN 提特征）
          → 只回 512 维特征 + 人脸框（约 11KB，**不上传图片**）
PC/Agent：特征 × 已登记的人 → 余弦相似度 → 得到姓名
          → 名字进入下一轮对话上下文 + 长期记忆
```

这么切的原因：整帧 JPEG 一帧 30~100KB，来回传一次就吃掉好几帧视频的带宽；
而名字（"这是王可旭"）只有 Agent 知道，设备没必要也不应该存。
**删掉 `artifacts/faces/face_db.json` 等于所有人脸都没登记过。**

### 怎么用

1. 控制台主页右栏有 **「人脸识别」** 面板（在「摄像头」下面）：
   点 **「扫一扫是谁」**，会列出每张脸的匹配结果与相似度；
2. 让对方面向摄像头，填名字后点 **「绑定这张脸」**；
3. 之后每轮对话开始前会自动扫一次脸，认出来的人名直接进 system prompt，
   机器人就会用名字称呼对方；
4. **聊天里听到身份信息就自动绑定**，不用手点（见下节）；
5. 面板里的人名按钮点一下即可删除该人的人脸绑定。

### 什么算「身份信息」：姓名，也可以是一个称呼

机器人不需要知道对方户口本上的名字 —— 只要能对上一个**稳定的称呼**，
就能一直认出这个人，并把之前聊过的内容算到他头上。所以下面这些都绑：

| 用户说的话 | 绑上的标签 |
|---|---|
| 我叫王可旭 / 我是李白 / 叫我小明 / 我的名字是… | `王可旭` |
| 我是**小明的爸爸** / 我是**小红的妈妈** | `小明的爸爸` |
| 我是爸爸 / 我是妈妈（不带名字的亲属称谓） | `爸爸` |
| 光说一句「**小明的爸爸**」（在回答"你是谁"） | `小明的爸爸` |

同时会写进长期记忆（`用户是小明的爸爸（已绑定人脸）`），
所以**即使以后没拍到脸**，也能凭这个称呼对上人。

两条刻意的边界：

* **纯职业不绑**：说"我是老师""我是工程师"不会绑脸 —— 否则一张脸被挂成
  "老师"，下一个老师进来就会被认成同一个人。职业只有在挂在名字下面时
  才算身份（"我是小明的老师"可以）。
* **第三人称不自动绑**：说"他是小明的爸爸"不会自动绑（画面里好几张脸时
  不知道绑谁）。这种情况模型会自己调 `bind_face` 来绑当前最大的那张脸。

判断顺序也有讲究：关系型规则必须排在姓名规则前面，否则"我是小明的爸爸"
会被姓名规则截成「小明的爸」当成名字（实测踩过）。

### 工具（模型可主动调用）

| 工具 | 说明 |
|---|---|
| `who_is_here` | 看一眼面前是谁，返回已登记的人名（认不出就说不认识） |
| `bind_face(name)` | 把眼前最大的那张脸绑到 `name` 上（姓名或「小明的爸爸」这类称呼都行），并写进长期记忆 |
| `forget_face(name)` | 删掉某人的人脸绑定（用户的生物特征控制权） |

### 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/faces` | 人脸库概览（人数、阈值、每人几条特征；**不含特征本身**） |
| POST | `/api/faces/scan` | 现扫一帧并返回匹配结果 |
| POST | `/api/faces/enroll` | `{"name":"王可旭"}` 绑定当前最大的脸 |
| DELETE | `/api/faces/{name}` | 删掉某人 |
| POST | `/api/faces/clear` | 清空人脸库 |

### 配置

| 字段 | 默认 | 说明 |
|---|---|---|
| `face.enabled` | `true` | 总开关 |
| `face.path` | `artifacts/faces/face_db.json` | 人脸库文件（相对**项目根**） |
| `face.threshold` | `0.5` | 余弦相似度阈值，沿用 esp-dl 的默认值 |
| `face.auto_scan` | `true` | 每轮对话前自动扫脸（设备端约 0.5 秒，可关） |
| `face.auto_enroll` | `true` | 聊天里听到姓名/关系称呼时自动绑脸 |
| `face.max_samples` | `8` | 同一个人最多存几条特征（不同角度取最高分） |

### 关于阈值

特征在设备端已 L2 归一化，**点积就是余弦相似度**；`0.5` 直接沿用
esp-dl `HumanFaceRecognizer` 的默认阈值（同一个模型，判据一致）。
换人脸模型（MFN ↔ MBF）必须**重新标定** —— 不同模型的相似度尺度不可比。

### 认不出来时先看「画面亮度」

`/api/faces/scan` 会回一个 `mean_luma`（设备端算的画面平均亮度 0~255）。
它把两种"检测到 0 张脸"区分开：

* **太暗**（`mean_luma < 20`，`too_dark: true`）—— 镜头前可能有人，但光不够。
  实测近全黑是 8，正常室内 29~48。此时工具和模型都会说「太暗，请开灯」，
  而不是误报"没人"。
* **正常亮度但没有脸** —— 那就是真的没人（或者人没在画面里）。

这条诊断是踩坑加上的：曾经**预览正常、抓拍却近全黑**（OV2640 每次
`set_framesize` 都会让 AEC 重新收敛），人脸自然一个都认不出。
详见 `sparkbot-esp32/README.md` 的坑 16。

另外还会区分「设备说检到脸」和「真正能用的脸」：接口同时回 `count`
（可用）与 `reported_count`（设备自报），两者不一致时会直接说
「特征数据不完整，疑似固件与 PC 版本不匹配」，不会显示成"没人"
（固件曾经因为 base64 长度查错，把 `feat_b64` 整个漏掉又不报错 —— 坑 17）。

### 实测数据（真板子 OV2640 @640x480）

| 项 | 实测值 |
|---|---|
| 单张脸检测分 | 0.98 ~ 0.999 |
| 同一个人两次的相似度 | **0.72**（阈值 0.5，留有余量） |
| 一次推理耗时 | 解码+检测 ~100ms，提特征 ~880ms，合计约 1.0~1.4s |
| 单帧检出率 | 不是 100%，人脸转动/眨眼那一帧可能漏检，**多扫几次** |

### 离线验证（不需要硬件，也不需要真人）

```powershell
python tests/test_face_recognition.py     # 34 条断言：相似度、绑定、自动绑名、删除、降级
```

模拟设备（`sparkbot/mock_device/`）会按名字哈希生成**带噪声**的 512 维特征，
所以"同一个人相似度高、换个人就认不出"这两个关键性质都能被稳定复现；
运行时还能用 `config` 指令换场景里的人（`{"face_people":["李四"]}`）。

---

## 机器人回「我的大脑有点连不上」？

这是**模型调用失败**的兜底回复。先看服务日志里那条 provider 报错：

```powershell
Select-String -Path logs\server_live.err.log -Pattern "provider 失败" | Select-Object -Last 3
```

### 如果是 HTTP 400 + `role 'tool' must be a response to ... 'tool_calls'`

会话历史里出现了**孤儿 `tool` 消息**：带 `tool_calls` 的 assistant 消息被
历史裁剪丢掉了，它的工具回复却留着。OpenAI/DeepSeek 会直接拒掉整个请求。

这条极难自己想到，因为现象跟"模型连不上"一模一样：唤醒、录音、识别全都正常，
只有回答变成"我的大脑有点连不上"，很容易误判成网络问题或唤醒坏了。

根因是会话记忆用定长 `deque` **逐条**裁历史。现在 `Memory.messages()` 会先做
一遍配对整理（`_sanitized`）：孤儿 tool 回复、以及没收到回复的 `tool_calls`
整组丢掉。宁可少一轮上下文，也不让模型调用失败。

回归测试：`python tests/test_chat_memory.py`（8 条断言，覆盖两个方向 + 正常历史不受影响）。

### 如果是超时 / 连接被拒

先确认这台机器能不能访问模型 API（本机实测过 GitHub 会解析到被墙的 IP，
模型 API 也可能遇到同类问题）：

```powershell
curl.exe -sS -m 10 -o NUL -w "%{http_code}`n" https://api.deepseek.com
```

## 语音唤醒不灵时按这个顺序查

唤醒检测在**设备本地**跑（esp-sr WakeNet），PC 只负责收到 `wake_word` 事件后
开始采集。所以先分清是"没喊中"还是"喊中了但没反应"：

1. **看设备有没有命中**。串口里搜 `唤醒词命中`；或者调
   `POST /api/action {"action":"wakeword_dump"}` 直接打印
   `feed / 命中 / 丢弃` 计数。`feed` 在涨、`丢弃=0` 说明检测器是活的。
2. **喊的词对不对**。当前模型是 `wn9_hixiaoxing_tts`，**只认「Hi,小星」**
   （英文 Hi + 中文小星）。「你好小星」「小星」「嗨小星」都不会触发。
3. **人声电平够不够**。每轮采集都会打一行
   `采集结果: ... 麦克风 峰值=N RMS=M`：

   | 场景 | 峰值 | RMS |
   |---|---|---|
   | 安静房间底噪 | ~200 | ~100 |
   | 喇叭在 10cm 处放测试音 | ~2200 | ~1030 |
   | 对板子正常说话 | 几千 | 几百以上 |

   峰值长期低于 1000，说明说得太轻或离得太远：要么凑近到 30~60cm，
   要么把 `SPARKBOT_AUDIO_MIC_GAIN_DB` 从 30 调到 36（ES8311 支持
   0/6/12/18/24/30/36/42 档）。
4. **别在机器人说话时喊**。播报期间会忽略唤醒（回声自触发保护），
   而且一旦唤醒就进入"对话态"（见下节），这期间麦克风本来就是开着的，
   直接说话即可。

## 对话态：唤醒一次，连续说，安静 5 分钟自动退出

设备只在收到 `start_listen` 时才上传音频，所以"唤醒 → 问一句 → 回复"跑完
麦克风就关了 —— 用户接着说第二句时**没有任何回应**，这就是早期的
"唤醒后只能对话一句"。现在的行为：

```
喊「Hi,小星」→ 叮 → 进入对话态（可以一直说，不用再喊）
                      ↓ 安静 5 分钟没人说话
                  自动退出对话态（表情切到 sleepy），回到"要喊唤醒词"的状态
```

| 字段 | 默认 | 说明 |
|---|---|---|
| `behavior.voice_session_idle_timeout_s` | `300` | 多久没人说话就退出对话态（0 = 关掉这个机制） |
| `behavior.voice_session_turns` | `0` | 轮数上限，`0` = 不限 |
| `behavior.voice_session_gap_s` | `1.2` | 轮间停顿（让喇叭把话说完 + 给用户反应时间） |
| `behavior.speech_min_rms` | `250` | "有声音"的电平门槛，低于它按静音处理 |

`/api/status` 的 `voice_session` 字段、控制台状态条上的「语音 开·对话中」
都能看出当前是不是处于对话态。

### 三个不做就会出事的细节

**① 静音轮不能进 ASR。** 会话态下每轮都会走完整流程，而 ASR 对纯静音经常
吐出一个 `'.'` 这样的**非空文本**，模型就会接话 —— 实测机器人对着空气每
几秒插一句"我就守在这儿，没动～"。所以进 ASR 之前先用麦克风电平挡一道
（`speech_min_rms`）。实测：安静房间 RMS≈100，人对着板子说话 RMS 几百以上。

**② 播报必须等设备播完再返回。** 否则下一轮采集会把机器人**自己的声音**
录成"用户说话"——实测第二轮识别出「次好吗？」，正是上一句
"你再说一次好吗？"的尾巴，机器人开始跟自己聊天。现在最后一段播报也等
`audio_done`（`_speak_segment(wait=True)`）。

**③ 听漏了也要算"有人在说话"。** 识别结果为空、但麦克风电平明显有语音时
照样给会话续期，否则一句话被漏识别就当场掉线，用户会觉得"又只能问一句"。

**④ 音频上行只在会话开始时开一次。**

麦克风（以及设备本地的唤醒检测）本来就是常开的；`start_listen` 只打开"音频
上行"。早期实现每轮都开关一次，等于每隔几秒就有一次**听不见的盲区** ——
用户刚好在这时开口，前半句就没了。现在的时序是：

```
喊「Hi,小星」→ 叮 → start_listen（一次，超时 = 静音窗口 + 60 秒）
      ├─ 静音：上行一直开着，PC 每 ~3 秒切一个窗口做电平判断（不连 ASR）
      └─ 听到人声 → stop_listen → 识别/回复/播报（等播完）→ start_listen
```

真机实测（串口）：

```
I (69021)  ← command play_tone       叮
I (69471)  ← command start_listen    开一次
I (112211) ← command stop_listen     43 秒后（检测到人声）才关
I (112991) ← command face_identify   进入这一轮
I (120131) ← command play_audio      播报
（播完 → 自动再 start_listen，回到聆听）
```

### 成本

静音期间上行是开着的：每 ~3 秒一个判断窗口、约 100KB 音频，5 分钟约 6MB。
**只有电平过门槛的窗口才会连 ASR / 进模型**，静音窗口的开销只有音频上行
（流式 ASR 会话也按需才连 —— 见下）。

想更省可以让固件用本地 VAD 触发（检测到人声再开上行），静音时零上行 ——
需要一个固件事件，目前没做。

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

本地 `speech-service/` 默认跑 **MOSS-TTS-Nano**（离线、纯 CPU），对应配置是
`SPARKBOT_SPEECH_TTS_MODEL=moss`、`SPARKBOT_SPEECH_TTS_VOICE=Junhao`。

全部配置项见 `.env.example`，也可以在控制台「设置」页里改其中大部分。

#### 流式识别与流式合成（默认走本地 `speech-service/`）

「流式」在这里是**两件不同的事**，各自解决的延迟也不一样：

| 环节 | 流式什么 | 实测收益 |
|---|---|---|
| ASR | 边说边推 PCM，**边说边出 partial 文本** | 话一说完就有文本，省掉整段识别的往返 |
| TTS | **边合成边下发** PCM，设备边收边播 | 首字延迟从"整句合成完"变成"第一块合成完" |

实测（sherpa-onnx + FunASR Paraformer-large，i5-11400）：

```
TTS 8.5 秒的句子：整段 2.9s → 流式首块 0.55s
ASR 5.6 秒的语音：话说完 → 最终文本 0.65s（partial 每 0.6s 一条）
```

协议（都是本地服务加的端点，OpenAI 官方没有；连不上会自动回退整段模式）：

* ASR：`WS /v1/audio/stream` —— 二进制帧推 16k/16bit/单声道 PCM，
  回 `{"type":"partial"|"final","text":...}`。
* TTS：`POST /v1/audio/speech/stream` —— 请求体同 `/v1/audio/speech`，
  响应是**裸 PCM**（无容器），按块返回。

**最终文本默认用 SenseVoice 复核**（`--stream-final batch`）：partial 由
流式模型实时给，句末再用精度更高的 SenseVoice 重识别一次整段。原因是流式
模型（Paraformer-online）会把"一台履带式"听成"你凯旅带式"，错字会让大模型
答偏；代价只有句末约 0.4 秒。要极致低延迟就改 `--stream-final stream`。

关掉流式：ASR 服务加 `--no-stream`；PC 端只要 provider 不支持流式（例如
直连 OpenAI 云服务）就会自动走原来的整段链路，不需要改配置。

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
sparkbot-esp32/     ESP32-S3 固件源码（ESP-IDF 工程，见其 README）
speech-service/     本地语音服务：ASR(SenseVoice + 流式 Paraformer) / TTS(MOSS-TTS-Nano)
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
