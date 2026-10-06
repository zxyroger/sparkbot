# SparkBot 设备协议 v1

PC 与 ESP32-S3 之间的完整通信约定。**固件实现以本文档为准**；
PC 侧的唯一事实来源是 `sparkbot/device/protocol.py`，两者必须保持一致。

---

## 1. 传输层

| 项 | 约定 |
|---|---|
| 传输 | WebSocket（RFC 6455），路径 `/robot` |
| 方向 | **设备主动连接 PC**（PC 是服务端，端口默认 8765） |
| 编码 | UTF-8 JSON 文本帧 |
| 心跳 | 设备每 `heartbeat_ms` 发一次 `telemetry`；PC 每 30 s 发 `ping` |
| 掉线判定 | 任一侧连续 3 个心跳周期无消息即断开 |
| 单帧上限 | 512 KB（文本）。更大的图像请降分辨率或改用二进制帧 |

设备连接地址形如 `ws://192.168.1.100:8765/robot`。设备需要知道 PC 的 IP；
如果 PC 的 IP 会变，建议在固件里做 mDNS 发现或把 PC 地址写入设备配置。

**二进制帧**：目前仅用于可选的原始音频透传。设备可以把 PCM 直接以二进制帧
发出，PC 会按 `audio.input_rate` 的裸 PCM 处理。**控制指令一律走文本 JSON**，
不要用二进制，否则无法调试。

---

## 2. 信封格式

所有消息共用一个信封。字段 `v` / `type` / `ts` 是所有消息的公共部分。

### 2.1 PC → 设备

**command（期望回复）**

```json
{
  "v": 1,
  "type": "command",
  "id": "a1b2c3d4e5f60718",
  "ts": 1735689600123,
  "action": "drive",
  "params": { "linear": 0.35, "angular": 0.0, "duration_ms": 1200 },
  "timeout_ms": 5000
}
```

设备**必须**回一条同 `id` 的 `result`。`timeout_ms` 是 PC 侧的等待上限，
设备可以忽略它。

**intent（不期望回复）**

```json
{ "v": 1, "type": "intent", "id": "...", "ts": ..., "action": "stop", "params": {} }
```

用于急停与高频遥操作。设备执行后**不要**回 `result`。

**ping / hello_ack**

```json
{ "v": 1, "type": "ping", "id": "...", "ts": ... }

{ "v": 1, "type": "hello_ack", "id": "...", "ts": ..., "ok": true, "error": null,
  "server": "sparkbot/0.1.0/1", "session": "s-9f2a1c", "server_ts": ...,
  "heartbeat_ms": 10000 }
```

`ok=false` 时设备应断开（常见原因：同 `device_id` 已被占用）。

### 2.2 设备 → PC

**hello（连接后必须立即发送，建议 3 秒内；PC 超时 5 秒断开）**

```json
{
  "v": 1,
  "type": "hello",
  "ts": 1735689600100,
  "device": { "id": "esp32s3-a1b2c3", "model": "ESP32-S3-N16R8",
              "fw": "0.1.0", "name": "小星" },
  "capabilities": ["camera", "microphone", "speaker", "display", "motor"],
  "display": { "width": 240, "height": 240, "color": "rgb565", "emotions": "full" },
  "camera":  { "max_width": 640, "max_height": 480, "formats": ["jpeg"] },
  "audio":   { "input_rate": 16000, "output_rate": 16000, "channels": 1 },
  "motor":   { "kind": "differential", "max_linear": 0.8, "max_angular": 2.5 }
}
```

* `device.id` **必填且必须稳定**。PC 用它做设备索引；同一 id 重连时
  旧连接会被踢掉（可配置为拒绝新连接）。
* `capabilities` **必填**。PC 用它决定向模型暴露哪些工具，
  也用它做安全钳制。宁可不报，也不要报自己不支持的字节。
* 能力名固定为：`camera` `microphone` `speaker` `display` `motor`。
  PC 侧忽略不认识的项，因此可以自行扩展。

**result（对 command 的回复）**

```json
{ "v": 1, "type": "result", "id": "a1b2c3d4e5f60718", "ts": ...,
  "ok": true, "data": { "applied": { "linear": 0.35 } }, "error": null }

{ "v": 1, "type": "result", "id": "...", "ts": ...,
  "ok": false, "data": {},
  "error": { "code": "unsupported_action", "message": "未实现 set_stream" } }
```

`error.code` 取值见第 6 节。

**telemetry（周期上报）**

```json
{ "v": 1, "type": "telemetry", "ts": ...,
  "battery": { "voltage": 7.9, "percent": 76 },
  "imu": { "yaw": 12.5, "pitch": 0.3, "roll": -0.1 },
  "motion": { "linear": 0.35, "angular": 0.0 },
  "display": { "face": "happy", "backlight": 100 },
  "audio": { "volume": 70, "listening": false },
  "led": [0, 128, 255],
  "rssi": -57, "uptime_ms": 812345 }
```

除 `type` 与 `ts` 外全部字段可选；PC 会原样保留并在 API 里透出。
`motion` 是唯一被 PC 主动读取的字段——用于「当前是否在移动」的判断。

**event（异步事件）**

```json
{ "v": 1, "type": "event", "ts": ..., "event": "wake_word",
  "data": { "phrase": "Hi,小星" } }
```

事件名见第 7 节。`wake_word` 与 `button` 会**触发 PC 侧的语音闭环**。

> `phrase` 必须填设备**实际配置**的唤醒词（见 7.1 节），
> 不是你想让用户喊的那句话。设备没有本地唤醒词时不会发这个事件。

**frame（图像）**

```json
{ "v": 1, "type": "frame", "id": "a1b2c3d4e5f60718", "ts": ...,
  "format": "jpeg", "width": 640, "height": 480, "seq": 42,
  "data_b64": "/9j/4AAQSkZJRgABAQ..." }
```

* 若这帧是对某条 `snapshot` command 的回复，`id` 填该 command 的 id；
  否则为 `null`（例如连续推流）。
* 发送顺序可以**先 frame 后 result，也可以先 result 后 frame**——
  PC 两种都支持。推荐先发 frame：PC 收到帧时若请求仍挂着，
  会立即完成该请求，省一次往返。
* `seq` 单调递增，用于检测丢帧。

**audio（音频）**

```json
{ "v": 1, "type": "audio", "ts": ..., "phase": "start",
  "format": "pcm_s16le", "sample_rate": 16000, "channels": 1 }

{ "v": 1, "type": "audio", "ts": ..., "phase": "chunk", "seq": 3,
  "format": "pcm_s16le", "sample_rate": 16000, "channels": 1,
  "data_b64": "..." }

{ "v": 1, "type": "audio", "ts": ..., "phase": "end" }
```

**一个采集会话必须且只能发一个 `end`。** 反复出现 `end` 会让 PC 侧的
采集逻辑误判会话已结束（这是一个真实踩过的坑：正常结束和被 `stop_listen`
取消各发一次，导致下一次采集刚开始就拿到空音频）。

推荐分片 20 ms（16 kHz 单声道 16-bit 即 640 字节/片）。

**pong** — 对 `ping` 的回复：

```json
{ "v": 1, "type": "pong", "id": "<ping 的 id>", "ts": ... }
```

---

## 3. 动作总表

单位统一为 SI：米、米/秒、弧度/秒、毫秒、摄氏度。

### 3.1 底盘（需要 `motor`）

| action | params | 说明 |
|---|---|---|
| `drive` | `linear`, `angular`, `duration_ms` | `duration_ms=0` 表示持续到下一条运动指令 |
| `stop` | `{}` 或 `{emergency: true}` | 立即刹停。intent 通道也支持 |
| `set_motion_limits` | `max_linear`, `max_angular` | 下发软限幅，固件应再做一层保护 |

`drive` 采用**开环定时**语义：到 `duration_ms` 自动停止并发
`motion_done` 事件。PC 侧会为这类命令放宽超时（时长 + 1.5 s）。

### 3.2 显示（需要 `display`）

| action | params | 说明 |
|---|---|---|
| `set_face` | `emotion`, `intensity` (0..1) | 见第 5 节表情表 |
| `set_text` | `text`, `duration_ms` | 在表情上叠加一行文字 |
| `display_frame` | `format`, `data_b64`, `encoding` | 直接推一整帧图片到 LCD |
| `set_backlight` | `percent` (0..100) | |
| `clear_display` | `{}` | |

### 3.3 视觉（需要 `camera`）

| action | params | 说明 |
|---|---|---|
| `snapshot` | `width`, `height`, `quality`, `format` | 抓一帧，以 `frame` 消息回传 |
| `set_stream` | `enabled`, `fps`, `width`, `height` | 连续推流（调试/监控用） |
| `set_camera_params` | 实现自定义 | 亮度、曝光等 |

### 3.4 音频

| action | 需要 | params | 说明 |
|---|---|---|---|
| `play_audio` | `speaker` | `format`, `data_b64`, `encoding`, `sample_rate` | 播放 PC 生成的音频 |
| `tts_speak` | `speaker` | `text` | 用板载 TTS 说话（可选能力） |
| `play_tone` | `speaker` | `frequency_hz`, `duration_ms` | 提示音 |
| `set_volume` | `speaker` | `percent` (0..100) | |
| `start_listen` | `microphone` | `timeout_ms`, `wake_word` | 开始采集并回传 `audio` 分片 |
| `stop_listen` | `microphone` | `{}` | 停止采集 |

`play_audio` 的 `format` 支持 `wav` / `mp3` / `pcm_s16le`。
裸 PCM 必须给 `sample_rate`。播放结束后应发 `audio_done` 事件。

### 3.5 系统

| action | params | 说明 |
|---|---|---|
| `set_led` | `r`, `g`, `b` | 板载状态灯 |
| `reboot` | `{}` | 设备重启；PC 收不到 `result` 是正常的 |
| `config` | 实现自定义 | 运行时改参数 |

---

## 4. 规范的安全要求

设备侧**必须**自行再做一层保护，不能只依赖 PC：

1. `drive` 的 `linear` / `angular` 与 `hello.motor.max_*` 取最小值后钳制；
2. 收到新的 `drive` 立即覆盖旧的定时停止；
3. `stop` 与 `intent` 必须无条件优先执行，不能排队；
4. 超过 `duration_ms` 必须自动停——不要依赖 PC 再发一条 `stop`；
5. 电量低于阈值时拒绝运动类指令并回 `low_battery`。

---

## 5. 表情表

`set_face.emotion` 的合法取值：

`neutral` `happy` `sad` `angry` `surprised` `sleepy`
`confused` `thinking` `love` `excited` `scared` `bored`

设备可以实现其中一部分，未实现的名字退化为 `neutral` 并仍需回 `ok=true`。
PC 侧遇到未知表情名也会退化成 `neutral`，不会报错。

---

## 6. 错误码

| code | 含义 |
|---|---|
| `unsupported_action` | 未实现的 action |
| `unsupported_param` | 认识的 action 但参数不支持 |
| `bad_params` | 参数缺失或非法 |
| `busy` | 正在执行其它动作 |
| `hardware_fault` | 硬件故障（摄像头初始化失败等） |
| `timeout` | 设备侧操作超时 |
| `low_battery` | 电量不足 |
| `internal` | 其它内部错误 |

`message` 应写清人类能读懂的原因，它会出现在日志与模型上下文里。

---

## 7. 事件名

| event | data | 说明 |
|---|---|---|
| `wake_word` | `phrase` | **触发语音闭环** |
| `button` | `name` | **触发语音闭环** |
| `touch` | `id` | |
| `obstacle` | `distance_m` | 前向测距 |
| `cliff` | | 掉落检测 |
| `bump` | `side` | 碰撞开关 |
| `low_battery` | `percent` | |
| `overheat` | `celsius` | |
| `error` | `code`, `message` | |
| `motion_done` | `action` | 开环定时走完 |
| `audio_done` | | 播报结束 |
| `listen_timeout` | | 静音超时，音频流自动结束 |

### 7.1 `wake_word` 的实现现状（重要）

`wake_word` 是**可选能力**，并非所有设备都能发。

* **能发的设备**：板载唤醒词引擎（如 ESP32-S3 + esp-sr WakeNet）在本地识别到
  唤醒词后上报。`data.phrase` 填**实际配置的唤醒词文本**，便于排查"是不是听错了词"。
* **不能发的设备**：没有本地唤醒词的设备**永远不会**发这个事件 ——
  此时语音闭环只能由 `button` 事件或 PC 侧手动触发（见 `POST /api/voice/trigger`）。

**参考实现（本仓库的 ESP32-S3 固件）**：

* 唤醒词是 **"Hi,小星"**（模型 `wn9_hixiaoxing_tts`）。
  官方开放词里**没有**"你好小星"，自定义训练需 ≥2 万条语料、2~3 周、付费。
* 使用 esp-sr **2.x**（本工程锁 2.4.7）。**1.9.x 走不通** —— 它只能手工填
  `AFE_CONFIG_DEFAULT()`，会漏掉 `afe_config_init()` 按输入通道格式推导出的
  内部参数，导致 AFE 的 `fetch()` 永远返回 `ret=-1, size=0`（不产出任何结果）。
* 上报的 JSON：

  ```json
  { "v": 1, "type": "event", "ts": ..., "event": "wake_word",
    "data": { "phrase": "Hi,小星" } }
  ```

* **限制**：当前固件的麦克风只在**采集窗口内**打开（codec 用静音位控制），
  所以唤醒词也只在采集期间被检测。要做到"休眠中喊一声就醒"，
  需要麦克风常开 + AFE 输入源改成常开采集流。这是已知的下一步。

> **不要在文档里写一个没有实现方会发的唤醒词。** 早期版本的协议文档举例写了
> `{"phrase": "你好小星"}`，而固件从未实现该词，容易让人误以为它能用 ——
> 示例值必须与真实实现一致，或明确标注为"未实现，仅为字段示例"。

---

## 8. 一次完整的交互时序

```
设备                                    PC
 │  ── hello ─────────────────────────►  │
 │  ◄──────────────────── hello_ack ──   │
 │  ── telemetry (每 heartbeat_ms) ────► │
 │                                        │
 │  ── event: wake_word ──────────────►  │   （或 PC 侧 /api/chat）
 │  ◄──────────── command: play_tone ──   │   即时反馈
 │  ◄──────────── command: start_listen ─ │
 │  ── audio: start ──────────────────►  │
 │  ── audio: chunk × N ──────────────►  │
 │  ── audio: end ────────────────────►  │
 │  ◄──────────── command: stop_listen ─  │
 │                                        │   ASR → Agent（可能多轮工具）
 │  ◄──────────── command: snapshot ────  │   ← 模型想看东西
 │  ── frame (id=snapshot 的 id) ─────►  │
 │  ── result (id=snapshot 的 id) ────►  │
 │  ◄──────────── command: set_face ────  │   ← 模型换表情
 │  ◄──────────── command: play_audio ──  │   ← 播报回复
 │  ── result ────────────────────────►  │
 │  ── event: audio_done ─────────────►  │
```

---

## 9. 固件实现检查清单

- [ ] 连接后 3 秒内发 `hello`
- [ ] `capabilities` 与实际硬件一致（不多报）
- [ ] 每条 `command` 都回一条同 `id` 的 `result`
- [ ] `intent` 不回 `result`
- [ ] `drive` 到点自动停，并覆盖旧定时器
- [ ] 每个音频采集会话只发一个 `end`
- [ ] `snapshot` 先发 `frame` 再发 `result`（推荐）
- [ ] `ping` 回 `pong`
- [ ] 按 `heartbeat_ms` 周期上报遥测
- [ ] 未知 action 回 `unsupported_action` 并附 `message`
- [ ] 收到非法 JSON 时忽略并继续，不要断开
