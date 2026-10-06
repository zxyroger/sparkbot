# 语音闭环问题追踪

**状态**：唤醒词持续监听、连续对话、本地 ASR/TTS、DeepSeek 全部打通。
剩余问题是**连接会周期性断开**，表现为屏幕上出现 `LINK LOST`。

---

## 已修复并实测确认的部分

| 能力 | 实测证据 |
|---|---|
| 唤醒词持续监听 | 设备空闲时 `feed` 持续增长，随时可喊 |
| 一次唤醒连续多轮对话 | `连续对话：第 1/3 轮结束` → `第 2/3 轮结束` |
| 真实识别（SenseVoice 本地） | `'很难看这个脸。'` |
| 真实回复（DeepSeek） | `'好啦好啦，回到最经典的开心脸～ 这张应该挑不出毛病了吧？'` |
| 真实语音播报（edge-tts） | `play_audio` → `播放结束`，可连续 3 轮 |
| 显示刷屏无错误 | SPI 错误 0 次 |

---

## 已修复：每 30 秒断连一次（屏幕显示 LINK LOST）

### 根因：我在修 ping 时引入的 `NameError`

固件每 30 秒主动 ping（`PING_INTERVAL_MS = 30000`）。PC 的
`DeviceConnection.handle()` **没有 ping 分支**，只把它当"未处理的类型"
记一条警告而不回应。补上 ping→pong 分支后，**我误用了 `conn` 而不是 `self`**：

```python
# DeviceConnection.handle() 里应该是 self，写成 conn 会抛 NameError
await conn.send({... "type": MsgType.PONG.value ...})
```

方法早先是模块级分发写法，改到类里后遗留了这个名字。后果：

```
WARNING gateway: 设备 esp32s3-a414c8 会话异常结束: name 'conn' is not defined
INFO    gateway: 设备 esp32s3-a414c8 断开: 会话结束
```

`NameError` 被 `_receive_loop` 的异常捕获 → **关闭连接**。而它只在
收到设备 ping 时触发，于是断连周期精确等于 ping 间隔：

```
21:13:41 断开: 会话结束
21:14:11 断开: 会话结束      ← 正好 30 秒
21:14:41 断开: 会话结束      ← 正好 30 秒
```

> 这也解释了为什么断开原因是 `会话结束`（接收循环退出）而**不是**
> 看门狗的 `心跳超时` —— 不是超时淘汰，是循环里抛异常退出了。

### 修复效果（实测）

```
等 100 秒（跨 3 个心跳周期）：
  断连次数       : 0
  会话异常结束   : 0
  未处理类型警告 : 0
设备 uptime      : 596 秒（连续在线 10 分钟）
```

修复前每 30 秒断一次，现在**完全不再周期断连**。

### 教训

`conn` / `self` 这种名字错用，在静态检查下能轻松发现（`ruff`/`mypy`
会直接报 undefined name）。**改完代码应当跑一遍 lint**，而不是只跑功能
测试 —— 这个分支只在设备发 ping 时才走到，功能测试不会覆盖。

---

## 已修复：断连根因是固件接收路径把"读到一半超时"当成断线

这是纠结最久的一个 bug，最终靠**分段日志**定位。完整因果链如下。

### 定位过程

1. **加 `_express()` 播报日志**（合成/下发分界点）→ 证明
   `播报: 合成完成 258092 字节` → `下发完成`，PC 侧一切正常。

2. **加 `_receive_loop` 退出分类日志** → 得到决定性一行：

   ```
   WARNING gateway: 设备 ... 接收循环退出: 对端发送 close 帧 (code=1005 reason=无)
   ```

   **是设备主动关闭**，不是 PC 关的。

3. **在 `bot_net_disconnect()` 里打调用方返回地址** → 锁定调用点：

   ```
   W app: 与 PC 的连接断开              ← app_main.c:278
   W bot_net: 关闭连接（调用方 0x4200ae34，sock=54）
   ```

   即"接收返回 `ESP_FAIL`"那条分支。

### 根因：`ws_recv_all()` 的一个判断缺陷

```c
if ((errno == EAGAIN || errno == EWOULDBLOCK) && got == 0) {
    return -2;   /* 空闲超时，不是错误 */
}
return -1;       /* ← 读到一半又超时，被判为"对端关闭"！ */
```

**只有当"一个字节都没读到"时，超时才被当作正常。** 而 PC 的 TTS
音频一帧 base64 后 **250KB 以上**，WiFi 上要读好几秒，`SO_RCVTIMEO`
极易在中途触发 → 返回 `-1` → 调用方认定"对端关闭" →
主动 `shutdown()` + `close()` → **设备自己把连接掐了**。

这完美解释了所有现象：

* 为什么**总在播报阶段**断 —— 只有音频帧足够大
* 为什么**帧越大越容易触发**
* 为什么 PC 端看到的是 **close 帧 code=1005**（`shutdown` 不发 close 帧）
* 为什么固件日志里**没有**"发送失败"/"握手失败"

### 修法

给整帧接收一个**总时间预算**，只要还有进展就继续等：

```c
const int64_t deadline_us = esp_timer_get_time() + WS_RECV_FRAME_BUDGET_MS * 1000;
...
if (errno == EAGAIN || errno == EWOULDBLOCK) {
    if (got == 0) {
        return -2;                    /* 完全没数据：空闲，正常 */
    }
    if (esp_timer_get_time() < deadline_us) {
        continue;                     /* 读到一半：继续等这一帧 */
    }
    return -1;                        /* 有进展但迟迟读不完：才当链路故障 */
}
```

`WS_RECV_FRAME_BUDGET_MS = 15000`：远大于正常所需的几百毫秒，
又远小于 PC 端 90 秒的判死阈值。

### 修复效果（实测 3 轮语音）

```
与 PC 断开 : 0 次      ← 修复前每轮都断
整帧超时   : 0 次
play_audio : 5 次      ← 命令确实到达设备
播放完成   : 6 次      ← 音频真的播出来了
```

### 方法论教训（值得记住）

前面几轮排查之所以慢，是因为**把"猜"当成了"查"**：

* 我基于"内存不足"的假设写了**三版内存守卫**，全部无效，
  第一版还让屏幕彻底不刷新。而实测数据（DMA 还有 25KB，只要 1.2KB）
  从一开始就否定了那个假设。
* 断连原因一直只显示笼统的 `会话结束`，我没有**先去补齐日志**，
  而是在黑盒上反复推理。

**正确的顺序是：先把关键路径变成可观测的，再基于观察结论动手。**
这次加上三段日志（播报分界点、接收循环退出分类、断开调用方地址）
之后，问题在**一轮测试内**就定位了。

---

## 新发现：DeepSeek 偶发泄漏工具调用标记

修复断连后，出现了一个新现象：

```
第2轮 -> '时维九月，序属三秋。…\n\n<｜｜DSML｜｜ call'
```

模型把**工具调用的内部标记**当成正文输出了（`<|DSML| call ...`），
说明 DeepSeek 的 XML 风格工具调用没有被正确解析，原始标记漏到了回复里。

这不是断连问题，是 **LLM 工具调用解析**的问题，需要单独处理：

* 检查 DeepSeek 返回里 `tool_calls` 的解析是否完整
* 回复文本里若残留 `<|DSML|` / `<|...|>` 这类标记，应当剥离或重试
* 长回复（如本轮的《滕王阁序》）更容易触发，可能与 `max_tokens` 截断有关

---

## 早期记录：已修复并实测确认的能力

30 秒周期问题解决后，暴露出另一类断连。

### 对照实验（非常有价值）

`behavior.talking_animation` 只控制**自动推断表情**。它关闭后，
只有模型**主动调用** `show_emotion` 工具才会执行 `set_face`：

```python
if self.settings.behavior.talking_animation:
    explicit = any(t.name == "show_emotion" and t.ok for t in turn.tool_invocations)
    if not explicit:
        emotion = infer_emotion(turn.reply)      # ← 每轮回复都自动做表情
```

对比 3 轮语音的结果：

| 轮次 | talking_animation=true | talking_animation=false |
|---|---|---|
| 第 1 轮 | 断连 | **无断连** |
| 第 2 轮 | 断连 | **无断连** |
| 第 3 轮 | 断连 | 断连（该轮模型主动调了 `show_emotion`） |

**结论：断连与 `set_face` 强相关。** 关闭自动表情后，没有表情的轮次
不再断连。

**注意 `set_face` 计数不为 0**：即使关掉 `talking_animation`，
模型自己调用 `show_emotion` 工具时仍会执行 —— 所以不能简单理解为
"关了表情就完全没 set_face"。

### 下一步：在 set_face 路径上加详细日志

既然锁定了 `set_face`，就缩小到这条路径：

1. **PC 侧**：给 `_receive_loop` 的退出加详细日志，区分是
   * 收到对端 close 帧
   * 协议违规
   * `receive()` 抛异常（打印类型与消息）

2. **固件侧**：在 `set_face` 处理前后打印
   * 各任务栈水位（`uxTaskGetStackHighWaterMark`）
   * 内部 RAM / DMA 余量

   重点看**是否是某个任务栈溢出**导致重启 —— 但注意：串口**没有**
   panic / backtrace 输出，且静置观测 8 次（32 秒）`uptime` 单调增长、
   **重启 0 次**，所以"重启"这个假设证据不足。

3. **确认命令回复路径**：`set_face` 会回一条 `result`。若这次 `send`
   走的是"真错误"分支并累计到 5 次，就会触发 `bot_net_disconnect()`。
   但实测 `发送失败 = 0`，所以这条也不成立 —— **需要更细的日志**。

### 已排除

| 假设 | 排除依据 |
|---|---|
| 设备重启 | 静置 32 秒 uptime 单调增长，重启 0 次；无 panic/backtrace |
| 30 秒心跳 | 已修复，`会话异常结束 = 0`、`未处理类型 = 0` |
| 发送失败 | 固件 `发送失败 = 0` |
| SPI/显示 | `SPI 错误 = 0` |
| WiFi 掉线 | `WiFi 断开 = 0` |

---

## 仍待查：`play_audio` 未到达设备

3 轮对话全部成功出文本回复，但固件侧 `播放完成 = 0`、`play_audio = 0`。
说明音频播报**根本没走到发送那一步**。可能是：

* `agent._express()` 里 `announce` 为 False，或 `ctx.tts is None`
* `robot.say()` 内部被大小上限拦下（PC 侧有 `max_b64` 检查）
* 断连发生在 `play_audio` 之前（时序上确实如此）

排查建议：在 `_express()` 的 TTS 合成与 `robot.say()` 前后各加一条
`logger.info`，就能立刻分清是"没合成"还是"没发送"。

---

## 早期记录：已修复并实测确认的能力

30 秒周期问题解决后，暴露出另一类断连：**每个语音轮次的播报阶段**断一次。

### 现象

```
I bot_proto: ← command set_face
W app: 与 PC 的连接断开              ← 约 3 秒后
```

PC 侧：

```
21:20:56 设备断开: 会话结束
21:20:56 播报失败: 设备已断开: 会话结束
21:20:56 语音交互完成: '嗯，背诵滕王阁序。' → '滕王阁序太长啦…'
```

**注意：识别和决策都成功了**（`'嗯，背诵滕王阁序。' → '滕王阁序太长啦…'`），
只是最后的播报没送出去。而且**不需要重启就能恢复**，下一轮照常工作。

### 已排除

* 设备崩溃 —— 串口无 panic / backtrace / 重启痕迹
* 30 秒心跳 —— 已修复，`会话异常结束 = 0`
* 发送失败 —— 固件 `发送失败 = 0`
* SPI/显示 —— `SPI 错误 = 0`

### 待查方向

1. **PC 侧给 `_receive_loop` 的退出加详细日志**，区分：
   * WebSocket 收到对端 close 帧
   * 协议违规
   * `receive()` 抛异常（打印具体类型与消息）

   目前只知道"循环退出了"，信息不足。

2. **确认 `set_face` 与播报的时序**：断连都发生在 `set_face` 之后约 3 秒。
   该命令会触发 LCD 刷新，而固件侧同时有音频播放任务与常开的 AFE。
   可以在 `set_face` 处理前后打印任务栈水位，看是否某任务溢出。

3. **对照实验**：临时把 `behavior.talking_animation` 设为 `false`
   （不自动做表情），看断连是否消失 —— 能判定是否与 `set_face` 相关。

---

## 早期记录：已修复并实测确认的能力

| 能力 | 实测证据 |
|---|---|
| 唤醒词持续监听 | 设备空闲时 `feed` 持续增长，随时可喊 |
| 一次唤醒连续多轮对话 | `连续对话：第 1/3 轮结束` → `第 2/3 轮结束` |
| 真实识别（SenseVoice 本地） | `'很难看这个脸。'`、`'嗯，背诵滕王阁序。'` |
| 真实回复（DeepSeek） | `'好啦好啦，回到最经典的开心脸～'` |
| 真实语音播报（edge-tts） | `play_audio` → `播放结束`，可连续 3 轮 |
| 显示刷屏无错误 | SPI 错误 0 次 |

---

## 早期排除的假设（避免重复试）

| 假设 | 排除依据 |
|---|---|
| LCD 刷屏 DMA 分配失败 | 已分块刷屏修复，SPI 错误 0 次；症状仍在 |
| 帧缓冲非 DMA | 已改为 PSRAM+DMA（日志 `可 DMA=1`），无效 |
| 发送缓冲拥塞 | 已加发送超时 + 拥塞丢帧 + 连续 5 次才断；`发送拥塞=0` |
| play_audio 内存不足 | 已改为 PSRAM 分配，播报可连续 3 轮成功 |
| WiFi 掉线 | `WiFi 断开 = 0` 次 |
| 设备收 ping 后自己关连接 | 固件 ping 分支只回 pong 并 return，无断开逻辑；且无崩溃日志 |

---

## 另一处已修复的重要配置 bug

**嵌套的 `BaseSettings` 子模型不读 `.env` 文件。**

`Settings` 根对象有 `env_file=".env"`，但 `SpeechSettings` / `LLMSettings`
等子模型各自实例化时**不会继承**这个设置，只读环境变量。后果是
**控制台改的所有配置重启后全部丢失**：

```
SpeechSettings(_env_file=".env")  → listen_timeout_s=1.5, asr=openai   ✅
SpeechSettings()                  → listen_timeout_s=8.0, asr=mock     ❌
```

→ 修法：给每个子模型的 `SettingsConfigDict` 显式加上
`env_file=".env"` 与 `env_file_encoding="utf-8"`。

---

## 复现与观察

```bat
:: 三个服务
D:\dsh\sparkbot\speech-service\start_asr.bat          :: ASR (8760)
cd D:\dsh\sparkbot\speech-service && .venv\Scripts\python.exe tts_server.py --port 8761
cd D:\dsh\sparkbot && python run.py           :: 主服务 (8765)

:: 抓串口
cd D:\dsh\sparkbot\sparkbot-esp32
C:\Espressif\python_env\idf5.5_py3.13_env\Scripts\python.exe tools\serial_log.py COM15 90
```

观察 PC 日志 `断开` 的时间分布：若是严格 30 秒间隔 → 心跳路径；
若集中在语音轮次末尾 → 播报路径。

**状态**：edge-tts 与 SenseVoice 都已装好并实测通过，但**完整的语音对话闭环
还跑不通** —— 卡在一个设备与 PC 之间的交互问题上。本文件记录诊断结论。

---

## 已确认可用的部分

| 组件 | 状态 | 实测数据 |
|---|---|---|
| SenseVoice ASR（本地 CPU） | ✅ | RTF **0.124**，2.16s 音频识别 268ms |
| edge-tts TTS（本地服务包装） | ✅ | RTF **0.387**，15 字 → 3.53s 音频，1366ms |
| 主项目配置接入 | ✅ | `ASR=openai`、`TTS=openai`，`persisted` 正常 |
| 真实识别 | ✅ | 采到人声能识别出中文（如 `'弯请选择一个声母 p…'`） |
| 真实合成 | ✅ | 服务端返回合格 WAV（首字节 `RIFF`，24kHz 单声道） |

**两个服务单独测都没问题。**

---

## 卡住的地方：一轮语音对话中途连接断开

### 关键线索（第二轮实测）

把采集窗口从 12 秒缩到 5.5 秒后，**症状明显减轻但没消失**：

| | 12 秒窗口 | 5.5 秒窗口 |
|---|---|---|
| 音频分片 | 494~528 片 | **111 片** |
| 整轮耗时 | 16.9 s | **7.9 s** |

真正的问题出现在这行命令之后：

```
I bot_proto: ← command set_face          ← PC 让屏幕做表情
E spi_master: setup_dma_priv_buffer(1208): Failed to allocate priv TX buffer
E lcd_panel.io.spi: panel_io_spi_tx_color(395): spi transmit (queue) color failed
W app: 与 PC 的连接断开                   ← 3 秒后断开
```

**顺序很重要：SPI 错误在前，断连在后。** 所以根因是
**LCD 刷屏时分配不到 SPI DMA 缓冲（内部 RAM 不够）**，
进而把连接也拖断了 —— 而不是"断连导致显示报错"。

注意：启动时内部 RAM 有 247KB 富余，说明是**语音轮次期间**被吃到不足，
不是一直如此。

### 修复方向（按代价排序）

#### 1. 让 LCD 在内存紧张时优雅降级（最直接）

`bot_hw_display.c` 的 flush 路径目前不检查失败。应改成：

- `esp_lcd_panel_draw_bitmap()` 失败时**跳过这一帧**并记一次计数，
  而不是让错误持续刷屏；
- 表情是低频事件，丢一帧视觉上无感，但能避免错误雪崩。

#### 2. 设备侧音频上行改成"满则丢帧"

`bot_proto_send_audio()` 走 `bot_net_send_text`。TCP 发不出去时当前会
判定连接断开。应改成：socket 设合理 send timeout、队列满时**丢弃这一片
音频**（丢几片对 ASR 影响很小）、连续多次失败才判断连。

参考：唤醒词的帧队列已是这个思路（`FRAME_QUEUE_DEPTH` 满则丢弃），
效果很好。

#### 3. 找出谁在吃内部 RAM

需要在 `set_face` 前后打印内部 RAM 余量，定位是：

- AFE 的 PSRAM 分配里有内部 RAM 部分？
- 采集任务的 PSRAM 栈实际落在哪？
- 还是 SPI 驱动为每个排队事务分配 DMA 描述符导致碎片？

建议加一条诊断日志：在 `bot_display` 每次 flush 前后打印
`heap_caps_get_free_size(MALLOC_CAP_INTERNAL)` 与
`heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT)`。
**"最大连续块"比"总量"更关键** —— DMA 要的是连续内存。

#### 4. 已验证有效但不彻底的临时缓解

把 `speech.listen_timeout_s` 从 8 降到 1.5（采集窗口 12s → 5.5s）：
音频分片减少 78%、整轮时间减半。**能缓解，但不能根治。**

---

## 已修复/配好的部分（本轮）

### LLM 换成真实 DeepSeek ✅

```ini
SPARKBOT_LLM_PROVIDER=deepseek
SPARKBOT_LLM_MODEL=deepseek-chat
SPARKBOT_LLM_API_KEY=sk-...        # 已写入 .env（被 gitignore）
```

实测：

```
我:   你好，你叫什么名字
小星: 很高兴认识你！有什么想让我做的吗？   (工具=show_emotion, 2.6s)

我:   你能做什么
小星: 想试试哪个？我可以给你做个表情，或者听听你想说什么。  (1.7s)
```

**回复是真实生成的，而且会调用工具让屏幕做表情。**

> 注意：`/api/config/test` 返回的 model 是 `deepseek-flash`，
> 与实际对话用的模型不同 —— 那是测试端点的行为，不代表主链路。

### ASR + TTS 接入 ✅

| 环节 | 实测 |
|---|---|
| 识别（SenseVoice 本地） | RTF **0.124** |
| 合成（edge-tts 本地服务） | RTF **0.387** |
| 真实识别文本 | `'带点我家。'`、`'I.'` 等（确实来自麦克风） |
| 真实回复文本 | 见上 |

---

## 早期推断（已被上面修正）

最初以为根因是"TCP 背压 → 发送失败 → 判定断连"。缩窗口后症状减轻
**印证了背压确实是一个放大因素**，但 SPI DMA 分配失败出现在断连之前，
说明**内存不足才是主因**，背压是次要因素。

---

## 复现步骤

```bat
:: 1. 起三个服务
D:\dsh\sparkbot\speech-service\start_asr.bat          :: ASR 服务 (8760)
:: TTS 服务 (8761) 见下面"备注"
cd D:\dsh\sparkbot && python run.py           :: 主服务 (8765)

:: 2. 抓串口
cd D:\dsh\sparkbot\sparkbot-esp32
C:\Espressif\python_env\idf5.5_py3.13_env\Scripts\python.exe tools\serial_log.py COM15 60

:: 3. 触发一轮语音（另开终端）
curl -X POST http://127.0.0.1:8765/api/voice/trigger
```

预期：串口出现 `与 PC 的连接断开`，PC 日志出现 `播报失败`。

> **备注**：TTS 服务的启动脚本还没写（ASR 有 `start_asr.bat`）。
> 手动启动：
> ```
> cd D:\dsh\sparkbot\speech-service
> .venv\Scripts\python.exe tts_server.py --port 8761
> ```
> 注意 TTS **不要**带 ASR 那套 `HOME` 覆盖 —— 那会破坏其它依赖家目录的东西。

---

## 相关配置（已写入 `.env`）

```ini
SPARKBOT_SPEECH_ASR_PROVIDER=openai
SPARKBOT_SPEECH_ASR_BASE_URL=http://127.0.0.1:8760/v1
SPARKBOT_SPEECH_ASR_MODEL=iic/SenseVoiceSmall
SPARKBOT_SPEECH_ASR_API_KEY=local

SPARKBOT_SPEECH_TTS_PROVIDER=openai
SPARKBOT_SPEECH_TTS_BASE_URL=http://127.0.0.1:8761/v1
SPARKBOT_SPEECH_TTS_MODEL=edge-tts
SPARKBOT_SPEECH_TTS_VOICE=zh-CN-XiaoxiaoNeural
SPARKBOT_SPEECH_TTS_API_KEY=local
```

> `provider=openai` 表示"OpenAI 兼容协议"，**不是**必须用 OpenAI 官方。
