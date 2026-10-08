# SparkBot 固件（ESP32-S3）

对接 PC 端 SparkBot 框架的机器人固件：WebSocket + JSON 协议，
驱动摄像头、麦克风、喇叭、LCD 显示屏与底盘电机。

本工程为独立实现，只参考了硬件接口定义（引脚分配、外设型号、
供电拓扑）。代码全部重写，仅使用 espressif 官方组件（Apache-2.0），
因此本工程可整体以 MIT 许可发布。

---

## 状态

**已在真板子上端到端跑通**（COM15，ESP32-S3-N16R8）。

| 项目 | 状态 |
|---|---|
| 编译 | ✅ 通过，**零警告**，固件 949 KB（4 MB 分区，余量 77%） |
| 烧录运行 | ✅ 已在 COM15 实测 |
| WiFi → PC 连接 | ✅ **已连通**：板子 `192.168.0.100` → PC `192.168.0.106:8765`；实测连续 90 秒无断开 |
| 协议握手 | ✅ hello/hello_ack 完成，能力协商正确 |
| 遥测上行 | ✅ 每 5 秒上报 |
| 显示屏 | ✅ 表情、文字、背光均在真屏上生效 |
| 喇叭 | ✅ 音调播放正常 |
| 麦克风 | ✅ `start_listen` 69 ms 响应；10 秒采集 511 片 / 超时 0 |
| 电源 (AXP2101) | ✅ 可读；**本板未接电池**，故不声明 battery 能力 |
| 本地唤醒词 | ⚠️ **进行中**：模型/分区/AFE 全部就位，但 `fetch()` 超时导致尚未能唤醒 —— 见下方「唤醒词」一节 |
| 摄像头 (OV2640) | ⚠️ **传感器未在 I2C 上应答** —— 见下方排查记录 |
| 电机 | ⏸ 默认关闭（本板无电机驱动，需外接 H 桥） |

实测的完整链路验证：

```
设备: 小星 | 型号: ESP32-S3 | 固件: 1.0.0
能力: display, microphone, speaker
通信延迟: 0.2 秒前

POST /api/face  {'emotion':'love'}          → 屏幕换表情 ✓
POST /api/action set_text 'SparkBot OK'     → 屏幕显示文字 ✓
POST /api/action set_backlight 40           → 背光变暗 ✓
POST /api/action set_volume 85              → 遥测回读 85 ✓
POST /api/action play_tone 1047Hz           → 喇叭响 ✓
POST /api/action start_listen  69ms         → {'listening': True} ✓
POST /api/chat  '你好'                       → 模型调用 show_emotion ✓
```

---

## 快速开始

### 1. 编译

```bat
build.bat
```

> **为什么不用 `idf.py`？** ESP-IDF 的 `export.ps1` 会校验**全部**已装工具，
> 只要有一个坏的或缺失（比如没装 `riscv32-esp-elf-gdb`、或 `qemu-xtensa`
> 启动失败）就直接报错退出，连 xtensa 编译都做不了。而构建实际只需要
> xtensa 工具链 + cmake + ninja + esptool。`build.bat` 把这几样放进 PATH
> 并设好 `IDF_PATH`，跳过那层校验。
>
> 如果你的工具装在别处，改 `build.bat` 顶部的 `IDF_PATH` / `IDF_TOOLS_PATH`。

```bat
build.bat reconf          :: 改了 CMakeLists / 组件后重新配置
build.bat flash COM15     :: 编译并烧录
build.bat monitor COM15   :: 串口监视（Ctrl+] 退出）
build.bat full COM15      :: 编译 + 烧录 + 监视
build.bat menuconfig      :: 图形化配置
build.bat clean           :: 清理
```

### 2. 配置 WiFi 与服务器地址

**必填**，否则板子连不上 PC：

```bat
build.bat menuconfig
```

进入 `SparkBot 固件配置`：

| 配置项 | 说明 |
|---|---|
| `网络 → WiFi SSID / 密码` | 必须与 PC 在**同一局域网** |
| `服务器连接 → PC 端 IP` | **仅作为回退**，正常会自动发现（见下） |
| `服务器连接 → PC 端端口` | 默认 8765 |
| `设备身份 → 设备显示名` | 会出现在对话里 |

PC 上的服务端监听 `0.0.0.0:8765`，所以板子填 PC 的**局域网 IP**
（不是 127.0.0.1）。Windows 上可能需要放行 8765 入站防火墙。

#### 服务器地址自动发现（推荐，免去改 IP 重新烧录）

**PC 的 IP 由 DHCP 分配、会变**（实测从 `192.168.0.103` 变到 `.106`）。
以前每次变了都得改配置、重新编译烧录，很烦。现在固件会自动找服务器：

```
WiFi 已连接，IP = 192.168.0.106
发现服务器: 192.168.0.103:8765          ← 广播发现，约 100ms
服务器地址已更新为 192.168.0.103（广播发现）
连接 PC: 192.168.0.103:8765/robot
```

原理：板子把 `SPARKBOT-DISCOVER-V1` **UDP 广播**到 `8765`，PC 端服务
收到后原路回一个 `SPARKBOT-HERE <ip>`，板子用应答的**源地址**去连。
PC 端实现在 `sparkbot/discovery.py`，启动时会打印：

```
自动发现已就绪：UDP 0.0.0.0:8765（回应 SPARKBOT 探测）
```

发现失败时**回退到上面配置的 IP**，所以行为与没有这个功能时一致。

> **注意**：自动发现要求 PC 端服务**已在运行**（它同时监听 UDP 8765）。
> 如果先给板子上电、后启服务，板子会每 30 秒重试一次广播，服务起来后
> 会自动连上，不需要重启板子。

需要手动改回退地址时（不想开 menuconfig）：

```bat
python tools\set_server_host.py 192.168.0.103
build.bat build
```

### 3. 启动 PC 端

```bash
cd D:\dsh\sparkbot
python run.py
```

板子上电后应看到屏幕显示 `BOOT` → 连上后变 `LINK OK`，
PC 端控制台 http://127.0.0.1:8765/ 的「设备」栏出现这台机器人。

---

## 已实现的能力

固件在 `hello` 里**只上报真实可用的能力**（PC 据此决定向模型暴露哪些工具，
多报会让模型调用一个注定失败的工具）：

| 能力 | 实现 | 协议动作 |
|---|---|---|
| `display` | ILI9341/42C，320x240 RGB565 | `set_face` `set_text` `set_backlight` `clear_display` |
| `microphone` | ES8311 ADC，16 kHz PCM 分片上行 | `start_listen` `stop_listen` |
| `speaker` | ES8311 DAC，WAV / PCM 播放 + 音调 | `play_audio` `play_tone` `set_volume` |
| `camera` | OV2640 DVP，JPEG 抓帧与推流 | `snapshot` `set_stream` `set_camera_params` |
| `camera` + 人脸 | esp-dl 本地推理（MSR+MNP 检测、MFN 提特征） | `face_identify` |
| `battery` | AXP2101 电压 / 电量 / 充电状态 | 遥测 `battery` 字段 |
| `motor` | 通用双路 H 桥 + 差速 + 开环定时 | `drive` `stop` `set_motion_limits` |

**12 种表情**全部实现（几何绘制，不需要图片资源）：
`neutral happy sad angry surprised sleepy confused thinking love excited scared bored`

### 人脸识别（本地推理，只回特征）

参考 esp-who 的做法用 esp-dl，在 **S3 上本地推理**，`face_identify` 只回
512 维特征与人脸框（**不传图片**，约 11KB），名字的绑定在 PC 侧 ——
见 `sparkbot-esp32/main/bot_face_rec.h` 与 `sparkbot/perception/face.py`。

| 项 | 取值 | 说明 |
|---|---|---|
| 检测 | `MSRMNP_S8_V1` | MSR 出候选框 + MNP 关键点精修 |
| 特征 | `MFN_S8_V1` | 512 维 float32，**已 L2 归一化** |
| 耗时 | 约 0.4~0.5 秒/帧 | 实测 640x480：解码+检测 87ms，单张脸提特征约 250ms |
| 上限 | 每帧 4 张脸 | 再多既慢又没用 |

三个 esp-dl 组件的版本必须**一起钉死**（`main/idf_component.yml`）：
`human_face_recognition ==0.2.0`、`human_face_detect ==0.2.0`、`esp-dl ==3.1.0`。
版本错配的典型报错是 `DL_IMAGE_CAP_RGB_SWAP` 未定义一类，
光看错误信息完全猜不到是版本问题（踩过）。

另外 **`bot_face_rec_init()` 必须在网络起来之后调用**：模型常驻要占
约 840KB 内存，先初始化会把 WiFi 的 malloc 挤失败
（现象是 `esp_wifi_init → ESP_ERR_NO_MEM` + 无限重启）。

### 几个实现上的取舍

**codec 只在启动时打开一次**：`esp_codec_dev_open()` 会重配 codec 时钟与通路，
耗时可达数百毫秒。如果放在命令处理路径上（`start_listen` / `play_audio`），
很容易吃掉 PC 端等 result 的 5 秒预算 —— 实测 `start_listen` 因此报 409 超时。
改成启动时以「收发同时打开」配置一次，之后全部靠**静音位**控制：

* 开始采集 → 解除输入静音
* 停止采集 → 打开输入静音
* 有音频要播 → 解除输出静音；播完 → 静音

这样既没有重配置开销（`start_listen` 从超时降到 **69 ms**），
也不会出现「一开麦克风就把喇叭通路复位」导致的断音。
代价是 ADC/DAC 一直有时钟（约十几 mA），对 USB 供电的调试场景可接受。

**不播 MP3**：`play_audio` 收到 `mp3` 回 `unsupported_param` 而不是静默失败。
PC 端 TTS 用 `wav` 或 `pcm_s16le` 即可，需要的话会自动重采样到 16 kHz
（TTS 常输出 24 kHz，不重采样会变成 1.5 倍速）。

**不做板载 TTS**：`tts_speak` 回 `unsupported_action`。esp-sr 的 esp-tts
需要中文语音数据分区，而 PC 端的 TTS 质量更好也更灵活，走 `play_audio` 即可。

**不做 `display_frame`**：没内置 JPEG 解码器（省 Flash 与依赖），
回 `unsupported_action` 并说明原因。要显示图片可以给固件加 `esp_jpeg`，
或让 PC 端把图缩到屏幕尺寸后以 RGB565 推过来。

**中文不显示**：内置字库只有 ASCII（8x16 点阵，95 个字形，
由 `tools/gen_font.py` 生成）。`set_text` 收到中文会画成 `?`。
需要中文要么扩字库，要么走图片路径。

**不做本地唤醒词**：需要 esp-sr 的 WakeNet 模型。PC 端仍可靠自己的唤醒逻辑
驱动语音闭环；板子收到 `start_listen` 就开始上传音频。

**没有电池就不声明 battery 能力**：本板未接电池时 AXP2101 的 VBAT 寄存器
会给一个约 **400 mV 的噪声值**（不是 0）。直接上报等于告诉 PC「电量 0.4 V」，
比不报更糟。所以固件做了合理性校验（2.5~4.6 V 之外视为读不到），
并且只有真的检测到电池才在 `hello` 里声明 `battery` 能力。

---

## 硬件说明（这块板）

| 外设 | 型号 | 接口 | 关键引脚 |
|---|---|---|---|
| 主控 | ESP32-S3-N16R8 | — | 16 MB Flash / 8 MB PSRAM |
| LCD | ILI9342C | SPI | SCK=41 MOSI=40 DC=39 CS=21 BL=42 |
| 音频 | ES8311 | I2S + I2C | MCLK=38 BCLK=14 WS=13 DOUT=45 DIN=12 PA=47 |
| 摄像头 | OV2640 | DVP + SCCB | XCLK=8 PCLK=16 VSYNC=3 HREF=46 PWDN=48 D0..D7=7,5,4,6,15,17,18,9 |
| 电源 | AXP2101 | I2C 0x34 | 电量计 + 摄像头供电 |

**共用 I2C**：ES8311（0x18）、AXP2101（0x34）、OV2640 的 SCCB（0x30）
挂在同一对 `SDA=1 / SCL=2` 上。固件里由 `bot_hw_i2c.c` 统一建一条
`i2c_master` 总线，摄像头通过 `pin_sccb_sda = -1` + `sccb_i2c_port` 复用它。

> 这里踩过坑：早先按 esp32-camera 的默认做法让它自建 SCCB 总线，
> 会和 `i2c_master` 抢同一组 GPIO，日志报
> `GPIO 1 is not usable, maybe conflict with others`，且通信不可靠。

**摄像头供电**：OV2640 的三路电源挂在 AXP2101 上，**电压也必须设对**：

| LDO | 供给 | 电压 |
|---|---|---|
| ALDO2 | VDDCAM_3V3（I/O） | 2800 mV |
| BLDO1 | AVDD | 2800 mV |
| BLDO2 | DVDD | **1200 mV** |

早先的实现只置使能位、不改电压，而这块板冷启动时 DVDD 的电压寄存器
停在 2.8 V 档 —— 直接上电可能损坏模组。现在固件按上表先设电压再使能，
并在启动日志里回读核对：

```
I bot_power: 摄像头供电: 0x90=0x77  IO(ALDO2)=on@2800mV  AVDD(BLDO1)=on@2800mV  DVDD(BLDO2)=on@1200mV
```

---

## 调试记录：踩过的坑

这些都是**在真板子上才暴露出来**的，记录在此以免重复踩。

### 0. 黄色表情显示成粉红 —— 屏幕其实是 ILI9342C，不是 ILI9341

**症状**：表情脸的黄色 `0xFDC0` 在屏幕上显示成粉红。

**根因**：`esp_lcd_ili9341` 驱动**能驱动 ILI9342C**（两者指令集基本兼容），
但**默认颜色行为不同**：

| 控制器 | 反色 (INVON) |
|---|---|
| ILI9341 | **需要** |
| ILI9342C | **不需要** |

而驱动本身**不会**自动发 INVON，反色完全由调用方决定。原代码照 ILI9341
写死了：

```c
esp_lcd_panel_invert_color(s_d.panel, true);   /* 对 ILI9342C 是错的 */
```

**修法**：把「颜色通道顺序」与「反色」都做成 Kconfig 选项，实测出正确组合：

```ini
CONFIG_SPARKBOT_LCD_COLOR_BGR=y
# CONFIG_SPARKBOT_LCD_INVERT is not set     # ILI9342C 不反色
```

**怎么快速定位**：固件内置了颜色诊断动作，屏幕依次显示红/绿/蓝/黄/白/黑：

```bash
curl -X POST http://127.0.0.1:8765/api/action \
  -H "Content-Type: application/json" -d "{\"action\":\"colors\"}"
```

| 你看到的 | 说明 | 改哪个 |
|---|---|---|
| 红色位置显示蓝色 | 通道顺序反了 | 颜色通道顺序改 RGB |
| 白显示成黑 | 反色开关反了 | 反色开关切换 |
| 红绿蓝都对、只黄不对 | 通道位数问题 | 需另查 |

启动日志里也会打印当前配置，便于对照：

```
bot_disp: 面板配置: 颜色顺序=BGR 反色=关
```

> **教训**：诊断动作**必须非阻塞**。第一版每色 `vTaskDelay(1200)`，
> 六色要 7 秒以上，而 PC 端命令超时是 **5 秒** —— 调用方只会拿到
> `未在 5.0s 内响应 colors`，"能跑但取不到结果"等于不可用。
> 改成状态机、由主循环推进后立即返回。

### 0.5 广播发现写崩了 —— 别猜，用 addr2line

做服务器自动发现时，设备**每次启动必崩**：

```
Guru Meditation Error: Core 0 panic'ed (LoadProhibited)
EXCVADDR: 0x00000200
```

崩在两条 `sendto` 之后，看起来就像 `recvfrom` 的问题。我据此做了两次
**错误**的猜测并且都改了代码：

1. 以为是栈溢出 → 把主任务栈从 8K 加到 16K，**无效**；
2. 以为是 `SO_RCVTIMEO` 不可靠 → 改成非阻塞轮询，**仍然崩**。

真正定位靠的是**解析 backtrace**：

```bat
xtensa-esp32s3-elf-addr2line -pfiaC -e build\sparkbot_firmware.elf 0x42075017 0x420751e7 0x4200e592
```

结果直接指出崩溃点根本不在 `recvfrom`：

```
ip4addr_aton   at lwip/ip4_addr.c:153
ipaddr_addr    (即 inet_addr)
discover_once  at bot_discovery.c:162     <-- 真正的崩溃行
```

**根因**是一段看着没问题的指针写法：

```c
const char *targets[] = {"255.255.255.255", NULL};
char subnet_bcast[32] = {0};        /* 声明在 targets 之后 */
targets[1] = subnet_bcast;          /* 指向后面的局部数组 */
inet_addr(targets[t])               /* 崩在这里 */
```

改成「两个固定缓冲区 + 计数遍历」后正常。

> **教训**：崩溃**现场**（"在 sendto 之后崩"）≠ 崩溃**原因**。
> 没有符号信息时，人的直觉很容易指向错误的函数；**先 addr2line 拿到
> 准确行号再改代码**，比反复"改一版试一版"快得多 —— 这次两次盲改
> 全是白费的。

### 1. `idf.py` 用不了 —— export 会被无关工具卡死

`export.ps1` 会校验**全部**已安装工具。这台机器上缺 `riscv32-esp-elf-gdb`
且 `qemu-xtensa` 启动失败，于是 export 直接报错退出，连 xtensa 编译都做不了。

→ 用 `build.bat`：只把需要的工具放进 PATH，并设好 `IDF_PATH` 与
`ESP_ROM_ELF_DIR`（后者不设会在 bootloader 配置阶段报
`ESP_ROM_ELF_DIR environment variable is not defined`）。

### 2. `.bat` 里写中文 → 全部变成乱码命令

`.bat` 存成不带 BOM 的 UTF-8 时，cmd.exe 按系统 ANSI/GBK 解析，
中文注释和 `echo` 被拆成乱码命令直接报错。

→ **所有 `.bat` 一律只写 ASCII**，中文说明放 README。

### 3. 字库生成写成了 UTF-16

`python tools/gen_font.py > main/bot_font.h` 在 PowerShell 里会把输出
写成 **UTF-16LE（带 BOM）**，GCC 报一屏 `stray '\377' in program`。

→ 生成脚本改成自己写文件并显式指定 `encoding="utf-8"`，
不再依赖 shell 重定向。

### 4. 摄像头与音频抢 I2C

按 esp32-camera 默认做法让它自建 SCCB 总线，会和 `i2c_master` 抢
`SDA=1 / SCL=2`，日志报 `GPIO 1 is not usable, maybe conflict with others`。

→ 摄像头配置 `pin_sccb_sda = -1` + `sccb_i2c_port = I2C_NUM_0`，
复用 `bot_hw_i2c.c` 建好的那条总线。日志出现 `Using existing I2C port` 即为成功。

### 5. 组件版本不兼容（最隐蔽的一个）

`esp32-camera` 用 `^2.0.0` 拉到了较新版本，那一版改用 **legacy I2C API**
（`i2c_param_config` / `i2c_master_cmd_begin`），与 IDF v5 的 `i2c_master`
驱动不兼容，`sccb_i2c_port` 指定的总线根本用不上。

→ 版本**锁死**为 `==2.1.7`（用新 `i2c_master`）。参考工程用的也是这个版本。

### 6. 摄像头供电只置使能位是不够的

原文档一句话让人以为「只动使能位，不改电压」。但 OV2640 的 DVDD 要求约
1.2 V，而这块板冷启动时 DVDD 的电压寄存器停在 **2.8 V** 档 ——
直接使能会让模组以错误电压上电。

→ 固件按板级配置先设电压再使能（ALDO2=2800mV / BLDO1=2800mV /
**BLDO2=1200mV**），并回读核对后打印。

### 7. 客户端把 recv 超时当成了断线

socket 设了 `SO_RCVTIMEO`，空闲时 `recv` 返回 `EAGAIN`。
原实现把 `EAGAIN` 一律当成致命错误 → 主循环判定「连接断开」→ 重连。
表现是**设备每 3 秒重连一次**，而链路其实一直是好的。

→ `ws_recv_all()` 区分三种返回：读满 / 出错或断开 / **空闲超时**。
只有后两者才断开。

### 8. `esp_codec_dev_open()` 阻塞导致命令超时

`start_listen` 在命令处理路径上调 `esp_codec_dev_open()`，
实测超过 PC 端 5 秒的等待上限 → 报 409 超时。

→ codec 改为启动时打开一次，之后只用静音位控制。`start_listen` 降到 69 ms。

### 9. 没有电池时报出「0.4 V 电量」

AXP2101 在未接电池时 VBAT 寄存器给出约 400 mV 的噪声值（不是 0）。
上报它等于告诉 PC「电量 0.4 V」。

→ 加合理性校验（2.5~4.6 V 之外视为读不到），并且只有检测到电池
才声明 `battery` 能力。**声明了能力却没数据，比不声明更糟。**

### 10. 采集任务栈溢出（把板子打重启）

`start_listen` 一执行，板子就 panic 重启。串口日志：

```
I bot_proto: ← command start_listen
I bot_audio: 麦克风采集开始: 16000Hz 分片 20ms (640 字节)
***ERROR*** A stack overflow in task bot_mic has been detected.
Backtrace: ... |<-CORRUPTED
rst:0xc (RTC_SW_CPU_RST)
```

根因是采集任务的栈给了 4096 字节，而它的调用链很深：

```
capture_task → 采集回调 → bot_proto_send_audio()
             → cJSON 建对象树 → mbedtls_base64_encode → bot_net_send_text
```

→ 栈提到 **8192**。现在 `start_listen` 16 ms 返回，采集全程 uptime 持续增长。

> 这个 bug 的表现很迷惑：PC 侧只看到 `start_listen` 报 409 超时，
> 而"命令超时"很容易被误判成网络或协议问题。**必须看串口日志**才能
> 看到 stack overflow 那行 —— 这也是为什么 `tools/serial_log.py` 值得留着。

### 11. I2S 全双工配错 —— 麦克风"没有声音"

这是最费劲的一个。现象：PC 侧采集永远拿不到音频，
`collect_audio` 报 `0 片 / 0 字节`，但板子看起来一切正常。

**根因是我把 `i2s_new_channel()` 的参数顺序搞错了**：

```c
// IDF 的签名是 (config, tx_handle, rx_handle)
// 我写成这样 —— 第三个参数是 RX，传 NULL 等于根本没建接收方向：
i2s_new_channel(&chan_cfg, &s_a.i2s, NULL);
// 之后 i2s_channel_read(s_a.i2s, ...) 永远读不到数据
```

中间还走过一次弯路：最初用 `i2s_new_channel(&cfg, &tx, &rx)` 建通道后
**同时启用两个方向**，看起来对，但播放任务持续写 TX 时会把 RX 严重饿死
（8 秒采集只读到 2 片），同样是"麦克风没声音"。

→ 正确做法是 IDF 文档里的全双工用法：**建通道时两个句柄都给**，
之后写用 tx、读用 rx。

修好后实测：

```
collect_audio 结束: 483 片 / 309120 字节 / end标记=0 超时=1
采集结果: 309120 字节 ≈ 9.66 秒（966 帧可识别）
语音交互完成: '（离线识别：这里是一句话）' → '我听到你说「…」…'
```

### 12. 采集窗口与静音阈值被混用

PC 侧 `collect_audio` 原来把 `silence_timeout_s`（静音多久算说完）
当成**设备侧的采集超时**下发：

```python
await self.start_listen(timeout_ms=int(silence_timeout_s * 1000))  # 只给 1.2 秒
```

结果设备 1.2 秒就自动收尾，PC 只能拿到 0~2 片音频。

→ 设备的采集超时应该由 `max_seconds`（用户最长说多久）决定，
两个参数是两件事。同时加了一个**保护期**（1.5 秒），
用来忽略上一轮遗留的 `end` 标记 —— 它可能在 `_drain_audio()` 之后才到，
不忽略的话新一轮采集会立刻"结束"。

### 13. `RobotProvider.try_get()` 的三元表达式写错

```python
# 错：conn 为 None 时仍然去读 conn.device_id
return self._cache.get(conn.device_id) if conn is None else (...)
```

传一个不存在的 `device_id` 就会抛 `AttributeError`，HTTP 层表现为 500。
→ 展开成普通分支。**这类"以为自己写对了"的一行式逻辑，
最好用反向测试覆盖**（`hw_test.py --negative` 里就有传不存在设备的用例）。

### 14. 人脸推理永远失败 —— JPEG 解码缓冲被算成了 0 字节

现象：调用 `face_identify` 一律回「人脸推理失败」，串口里是

```
E (133100) JPEG: esp_jpeg_decode(105): Not enough size in output buffer!
E (133100) dl_image_jpeg: sw_decode_jpeg(41): Failed to decode img.
```

根因：esp-dl 的 `sw_decode_jpeg()` **不解析 JPEG 头**，它直接拿
`jpeg_img_t` 自带的 `height * width * 3` 去申请输出缓冲
（见 `dl_image_jpeg.cpp`）。我们的代码只填了 `data` / `data_size`，
`width`/`height` 留成了 0 → 输出缓冲 0 字节 → 解码器自己报"缓冲不够"。

修法：先在 `bot_face_rec.cpp` 里用 `esp_jpeg_get_image_info()` 从 JPEG 头
读出真实尺寸再填进去。**不从调用方传参**是刻意的 —— 少一处"忘了传"的机会，
顺带还能挡掉"传进来的尺寸和实际 JPEG 不符"。

### 15. 人脸特征按 int8 读，读到的全是垃圾

第一版以为"模型是 int8 量化的，所以输出也是 int8"，于是
`feat->get_element<int8_t>(i)`。实际上 esp-dl 的 `FeatPostprocessor`
把特征转成了 **float32 并做了 L2 归一化**，所以按 int8 读等于把
float 数据的前 512 个字节重解释一遍 —— 数字完全随机，相似度没有意义
（而且**不会报错**，只会"永远认不出/偶尔认错"）。

正确做法：`feat->dtype` 就是 `DATA_TYPE_FLOAT`，直接
`const float *src = (const float *)feat->data;`。
顺带一个好处：**已归一化 → 点积就是余弦相似度**，PC 侧连归一化都省了，
阈值还能直接沿用 esp-dl `HumanFaceRecognizer` 的默认值 `0.5`。

### 16. 抓拍的帧近全黑 —— 每次 snapshot 都把 AEC 打回起点

现象：人脸识别永远「检测到 0 张」，而**推到 Web 的预览画面是正常的**。

量出来的数字（同一台设备、同一个房间、相隔几分钟）：

| 抓图路径 | 平均亮度 | 暗像素占比 | 帧大小 |
|---|---|---|---|
| 推流（`set_stream`，只设一次分辨率） | 31/48/28 | 5~8% | ~20KB |
| 抓拍（`snapshot`，每次都设分辨率） | 8~16 | 43~95% | ~9KB |

根因：`bot_camera_snapshot()` 每次都会把 PC 传来的 `width/height`
原样喂给 `sensor->set_framesize()`。OV2640 改分辨率会重写时序/窗口寄存器，
副作用是 **AEC/AGC/AWB 从头收敛**，之后若干帧都是"没收敛"的状态 ——
在室内光线下就是近全黑。推流路径只在开启时设一次，所以帧一直是亮的。

这条极难发现的原因：**预览（推流）正常、只有抓拍暗**，
而抓拍正是 `look_around` / `face_identify` / `/api/frame?fresh=true` 走的路。

修法：加 `apply_framesize_locked()`，**只有分辨率真的变了才写寄存器**，
并且写完丢 6 帧等 AEC 重新收敛。修完同一实验变成 mean 32~34、暗像素 14~16%、
六次抓拍几乎一样 —— 稳定了。

顺带把**画面平均亮度**也回传给 PC（`face_identify` 的 `mean_luma`）：
"检测到 0 张"到底是**没人**还是**太黑/镜头被挡**，光看检测结果分不出来，
但这两件事的处理方式完全不同（后者要开灯/调角度）。实测近全黑是 8，
正常室内 29~48，阈值取 20。

### 17. 用 `mbedtls_base64_encode(NULL, 0, &need, ...)` 查长度 —— 特征永远传不出去

现象极具误导性：串口里明明写着

```
latency: feat::forward: 860106 us
bot_face: 人脸识别: 检测到 1 张（640x480 亮度=100 解码+检测 103 ms，特征 877 ms）
```

也就是**检测到了脸、也提取出了特征**，可 PC 侧收到的回包里
`faces[0]` 只有 `x1/y1/x2/y2/score/feat_len`，**没有 `feat_b64`**，
于是 PC 侧把这张脸当"特征不可用"丢掉，最终报成「检测不到人脸」。

根因：`mbedtls_base64_encode()` 在目标缓冲为 `NULL` 时返回的是
`MBEDTLS_ERR_BASE64_BUFFER_TOO_SMALL`（同时把所需长度写进 `*olen`），
**不是 0**。而代码写的是

```c
if (mbedtls_base64_encode(NULL, 0, &need, src, len) == 0) {   /* 永远不会成立 */
    ... 真正的编码 ...
}
```

判断永不成立 → 编码整段被跳过 → 没有日志、没有报错、字段就是没了。

修法：**长度自己算**（`((n + 2) / 3) * 4 + 1`，含结尾 `\0`），一次编码到位。
同文件里的 `bot_proto_send_frame()` 本来就是这么写的，直接对齐即可。

留了两道防线，避免同类问题再被藏起来：
* 固件侧编码失败会打 `ESP_LOGW`；
* PC 侧区分「设备自报的脸数」和「真正可用的脸数」，
  `count` 与 `reported_count` 不一致时会明确报"特征数据不完整，疑似版本不匹配"，
  而不是显示成"没人"。

### 读串口时不要碰 DTR/RTS

ESP32-S3 的日志走**原生 USB**，与烧录同一接口，而 DTR/RTS 被用来做
复位与下载模式控制。PowerShell 的 `SerialPort` 打开端口时会翻转这两根线，
结果就是「一读日志板子就重启」，进而把命令测试也一起搞乱。

→ 用 `tools/serial_log.py`（pyserial，显式 `dtr=False, rts=False`）：

```bat
C:\Espressif\python_env\idf5.5_py3.13_env\Scripts\python.exe tools\serial_log.py COM15 20
```

### 别用 PowerShell 的 Set-Content 改含中文的源文件

`Set-Content -Encoding UTF8` 在本机把中文注释写成了乱码
（PowerShell 5.1 的 UTF8 会带 BOM 且按本地代码页解析源文本），
而且 `.bat` 用同样的方式写会直接被 cmd.exe 当成乱码命令。

→ 改源码用编辑工具（显式 UTF-8 写入）；`.bat` 一律只写 ASCII。

---

## 本地唤醒词「Hi,小星」

> 当前 `CONFIG_SPARKBOT_WAKE_WORD_ENABLE=y`（默认开启）。
> AFE 流水线**已跑通**（`fetch` 正常产出、无失败），采集链路不受影响。
> 端到端"喊一声 → 板子回话"的最终确认需要真人对板子喊（见文末）。

### 官方没有「你好小星」，但有「Hi,小星」

| 想要的 | 官方实际有的 | 说明 |
|---|---|---|
| 你好小星 | **Hi,小星**（`wn9_hixiaoxing_tts`） | 音最接近，零成本，**本工程采用** |
| 你好小星 | 你好小智 / 你好小鑫 | 后两个字不同 |
| 你好小星 | — | 自定义训练要 ≥2 万条语料、2~3 周、付费 |

「你好小星」这种自定义词**不在官方开放词里**。
[定制流程](https://docs.espressif.com/projects/esp-sr/zh_CN/latest/esp32s3/wake_word_engine/ESP_Wake_Words_Customization.html)
要求 >500 人、含儿童 ≥100 人、专业录音环境的语料，显然不适合本项目。

### 关键结论：必须用 esp-sr **2.x**，1.9.x 走不通

这是本功能最核心的一条经验，也是我花了三轮才定位到的问题。

| | esp-sr 1.9.5 | esp-sr 2.4.7（本工程现用） |
|---|---|---|
| 配置方式 | 只能手工填 `AFE_CONFIG_DEFAULT()` | **`afe_config_init(input_format, models, type, mode)`** |
| 结果 | `fetch()` 永远 `ret=-1, size=0` | `fetch()` 正常产出 |

1.9.5 下我按参考实现反复调整过：任务优先级（3/5）、`afe_ringbuf_size`（50/200）、
VAD/SE 开关、检测模式（`DET_MODE_2CH_90`/`DET_MODE_90`）、通道数（1/2）、
帧长（160/512/1024）、`LOW_COST`/`HIGH_PERF` —— **全都无效**。

根因是 **API 代际差异**：2.x 由 `afe_config_init()` 按输入通道格式
（`"M"` / `"MR"`）**推导**出整套内部参数（各帧长、ringbuf、内部任务栈等），
手工填 `AFE_CONFIG_DEFAULT()` 会漏掉这些推导，内部流水线就无法产出。

> 判断依据来自同芯片（ESP32-S3）、同 IDF（5.5.4）的
> [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) 工程 ——
> 它的唤醒词是正常工作的，用的正是 2.4.7 + `afe_config_init()`。

### 已跑通的证据

```
MODEL_LOADER: Successfully load srmodels
bot_wake: 分区里共有 1 个模型:  [0] wn9_hixiaoxing_tts
bot_wake: 使用唤醒词模型: wn9_hixiaoxing_tts
bot_wake: 该模型的唤醒词: Hi,小星
MC Quantized wakenet9: wakenet9l_tts1h8_Hi,小星_3_0.626_0.630, tigger:v4
AFE: AFE Version: (1MIC_V251128)
AFE: Input PCM Config: total 1 channels(1 microphone, 0 playback), sample rate:16000
AFE: AFE Pipeline: [input] -> |VAD(WebRTC)| -> |WakeNet(wn9_hixiaoxing_tts,)| -> [output]
bot_wake: 唤醒词就绪: 模型=wn9_hixiaoxing_tts feed帧长=512 采样(1通道) fetch帧长=512
bot_wake: AFE 进度: 已 feed 83 帧, 丢弃 0, 最近一帧 size=1024
```

`fetch` 正常产出（`size=1024`）、零丢弃 —— 内部流水线是活的。
（对比 1.9.5 时期：`fetch` 每次阻塞 2~3 秒返回 `ret=-1, size=0`。）

### 补了一个 esp-sr 漏掉的 Kconfig 选项

esp-sr 2.4.7 **自带** `model/wakenet_model/wn9_hixiaoxing_tts`
（284KB，"Hi,小星"），但**没有**在它的
`Load Multiple Wake Words (WakeNet9)` 菜单里开放对应开关。

而模型打包脚本 `model/movemodel.py` 是**纯按 sdkconfig 推导**目录名的：

```
CONFIG_SR_WN_WN9_HIXIAOXING_TTS=y
  → label.split("_SR_WN_")[-1].lower() = "wn9_hixiaoxing_tts"
  → 去 model/wakenet_model/ 找同名目录并打包
```

找不到目录时它**静默跳过**，最终产出 **0 字节**的 `srmodels.bin` ——
表现为分区挂载成功但一个模型都没有。

→ 在 `main/Kconfig.projbuild` 里补上同名 config（用 esp-sr 自带的模型数据），
`srmodels.bin` 就正常了：**284.2 KB**。

> 这类"静默跳过"最坑：构建成功、烧写成功、启动日志也不报错，
> 只有模型列表是空的。排查时**务必确认 `build/srmodels/srmodels.bin`
> 不是 0 字节**，并看启动日志里 `分区里共有 N 个模型` 的 N 是不是 0。

### 架构：为什么 feed/fetch 不在采集任务里

```
采集任务 ──(片)──> 帧队列 ──> AFE worker ──> feed()/fetch()
  (只投递,      (16 帧,满则丢)   (允许阻塞)
   绝不阻塞)
```

早期把 `feed()`/`fetch()` 直接放在采集任务里，而 `fetch()` 会阻塞
（1.9.5 下实测 3 秒；2.4.7 下虽快，但仍不应占用采集上下文）。
10ms 的音频帧要花数秒处理，采集被彻底饿死：**10 秒只有 2 次 I2S 读**。

解耦后采集恢复到 **400~560 次/10 秒**（`collect_audio` 511 片 / 10.22 秒 / 超时 0）。

### 麦克风常开：唤醒检测持续运行（重要修复）

**问题**：唤醒词只在"采集窗口"内生效 —— 麦克风平时是关的（靠 codec
静音位控制），采集任务绑定在 `listen_start/stop` 的生命周期上。

后果很荒谬：**唤醒词只能在"已经在听"的时候才能唤醒**，而那时用户早就
主动按了按钮。实测：设备空闲时对着它喊「Hi 小星」，串口**毫无输出**。

**修法**：把三件事拆开，各自独立控制。

| 关注点 | 变化 |
|---|---|
| 麦克风 + 采集任务 | **常开**（启动后就一直跑） |
| `monitor_cb`（唤醒检测） | **常开**（AFE 一直有数据） |
| `capture_cb`（上行给 PC） | 仅 `listen` 会话期间 |

新增两个 API：

- `bot_audio_start_monitor()` —— 启动时调用，常开麦克风但**不开上行**
- `bot_audio_set_uplink(bool)` —— 上行开关，由 `listen_start/stop` 控制

```c
/* 采集任务里：两个开关完全独立 */
if (s_a.monitor_cb != NULL) {
    s_a.monitor_cb(pcm, len);              /* 唤醒检测：常开 */
}
if (s_a.uplink_active && s_a.capture_cb != NULL) {
    s_a.capture_cb(pcm, len);              /* 上行：仅采集会话 */
}
```

**为什么必须分开**：若混为一谈，设备会把环境声音一直推给 PC，
白耗带宽与 PC 算力。所以要"麦克风一直听"但"音频不乱传"。

**配套改动**：

- `bot_proto_listen_start()` 不再 `bot_audio_capture_start()`，改为
  `bot_audio_set_uplink(true)`
- `bot_proto_listen_stop()` 改为 `bot_audio_set_uplink(false)`（**不关麦克风**）
- `bot_proto_on_disconnect()` 原本调 `bot_audio_capture_stop()` ——
  那会在断连时把唤醒能力也一起关掉，重连后就再也喊不醒。
  改成只关上行。

**验证**：设备空闲、不做任何操作时，串口持续输出

```
bot_wake: 唤醒检测运行中: feed=601 丢=0 size=1024 wakeup_state=0
```

`feed` 一直在涨 = 麦克风确实常开、AFE 一直在处理。

**实测效果**（用户真人喊「Hi 小星」）：

```
18:27:10 语音交互完成: '再做一个伤心的表情。' → '你看，我伤心的时候就是这样，嘴角都垂下来啦。'
18:27:32 语音交互完成: '.' → '想让我做表情，还是听你说话？告诉我一声就好。'
18:27:46 语音交互完成: '做一个惊讶的表情。' → '哇——眼睛瞪得大大的，是不是很惊讶？'
```

**喊一声就能开始对话，且支持多轮连续交互。**

> **日志噪音**：常开后 AFE 一直在跑，进度日志从每 200 帧改成每 2000 帧
> （约 20 秒一条），否则会把真正有用的信息冲掉。

### 三个导致"只能对话一次"的 bug（都已修复）

这一轮排查踩了很多弯路，把结论和**走过的错误假设**都记下来，
避免以后重复。

#### 表现

唤醒后能对话一次，之后再喊没反应。串口在每轮末尾出现：

```
E spi_master: setup_dma_priv_buffer(1208): Failed to allocate priv TX buffer
E lcd_panel.io.spi: panel_io_spi_tx_color(395): spi transmit (queue) color failed
W app: 与 PC 的连接断开
```

#### bug 1：LCD 刷屏一次性提交整块，DMA 临时缓冲分配失败

**机制**（读 IDF 源码 `spi_master.c` 的 `setup_dma_priv_buffer` 得到）：

```c
is_ptr_ext  = esp_ptr_external_ram(buffer);                    // 帧缓冲在 PSRAM → true
use_psram   = is_ptr_ext && (flags & SPI_TRANS_DMA_USE_PSRAM);
need_malloc = is_ptr_ext ? (!use_psram || !esp_ptr_dma_ext_capable(buffer)) : ...;
mem_cap     = MALLOC_CAP_DMA | (use_psram ? MALLOC_CAP_SPIRAM : MALLOC_CAP_INTERNAL);
```

**而 `esp_lcd` 的 SPI 驱动在 IDF 里从不设置 `SPI_TRANS_DMA_USE_PSRAM`**
（全 IDF 只有 SPI 自测代码用它，配置结构里也没有对应开关）。
所以只要传输缓冲在 PSRAM，驱动就会申请一块**内部 DMA 缓冲**再拷贝。
本板启动日志写着 `Reserving pool of 32K of internal memory for DMA`，
池子很小，整屏 320x240x2=150KB 的分配必然失败。

→ **修法**：把刷屏按 **16 行**分块，单块只有几百字节。

```c
const int kMaxRowsPerChunk = 16;
while (y <= y1) {
    int y_end = min(y + kMaxRowsPerChunk - 1, y1);
    esp_lcd_panel_draw_bitmap(s_d.panel, x0, y, x1 + 1, y_end + 1, s_d.fb + y * width + x0);
    y = y_end + 1;
}
```

**效果**：SPI 错误 0 次，DMA 可用内存从 25KB 升到 **126KB**（分块后不再长期占用）。

> **走过的弯路（重要教训）**：我前后写了三版"内存守卫"想把失败挡在前面，
> 全都基于错误假设：
> 1. 按整块图像大小（150KB）要求内部 RAM → 屏幕上**彻底不再刷新**，比原问题更糟；
> 2. 按 `MALLOC_CAP_INTERNAL` 最大连续块判断 → 放行，但失败照旧；
> 3. 按 `MALLOC_CAP_DMA` 判断 → 仍然放行。
>
> 实测数据是决定性的：**失败时 DMA 还剩 25KB、最大连续块 13.8KB，
> 而它只要 1208 字节** —— 根本不是"容量不够"，所以任何基于容量的守卫
> 都不可能生效。最后靠"把请求变小"（分块）解决。
>
> 教训：**在搞清机制之前不要写"防护"代码**。防护逻辑建立在错误模型上，
> 只会掩盖问题甚至制造新问题。

#### bug 2：帧缓冲没有 DMA 能力标志

即使把帧缓冲改成 `MALLOC_CAP_SPIRAM | MALLOC_CAP_DMA` 并 64 字节对齐
（日志确认 `在 PSRAM=1，可 DMA=1`），错误依旧 —— 因为如上所述，
`esp_lcd` 不设 `SPI_TRANS_DMA_USE_PSRAM`，`use_psram` 恒为 false。
**这个改动本身是正确的**（减少一次潜在的额外拷贝），可以保留，
但**它单独并不能解决问题**。

#### bug 3：唤醒词在采集期间重复触发

这个才是"只能对话一次"的直接原因。麦克风常开后，唤醒检测在采集期间
**照常运行**；用户接着说话时，内容里若出现与唤醒词相近的音就会再次触发。
实测用户说的 **"嗨小新"** 能触发「Hi,小星」：

```
I (50476) ← command start_listen        第一轮采集开始
I (54556) 唤醒词命中！（第 2 次）         ← 采集期间又命中！
```

于是 PC 端收到第二个 `wake` 事件，在上一轮还没播报完时又开一次采集，
两个采集会话互相打断 → 断开 → 播报失败：

```
20:40:39 语音交互完成: '你是嗨小新。' → '是记错名字啦？'
20:40:39 语音交互：开始采集              ← 立刻又开一轮
20:40:50 断开: 会话结束
20:40:50 播报失败: 设备已断开
```

→ **修法**：命中后加 **4 秒冷却期**（`WAKE_COOLDOWN_MS`），
这也是回声抑制的常规做法 —— 自己正在采集/播报时，不该把残留音频
当成新的唤醒。被抑制的次数会记在 `hits_suppressed` 里，便于观察。

```c
if (s_w.last_hit_us != 0 &&
    now_us - s_w.last_hit_us < (int64_t)WAKE_COOLDOWN_MS * 1000) {
    s_w.hits_suppressed++;
    continue;   /* 冷却期内忽略 */
}
```

**效果**（实测两次连续唤醒）：

```
命中    : 2 次
断连    : 0 次      ← 修复前是 1 次
SPI错误 : 0 次

命中 → play_tone → start_listen → 采集 → set_face → play_audio   ← 全程无断连
```

PC 端：

```
20:43:48 语音交互：开始采集
20:43:59 语音交互完成: '听到了吗？' → '还想聊点啥？'     ← 无播报失败
```

**多轮连续对话现在可以正常工作。**

#### 顺带修复：断连不再关麦克风

`bot_proto_on_disconnect()` 原本调 `bot_audio_capture_stop()`，那会在每次
断连时把唤醒能力一起关掉，重连后就再也喊不醒。改成只关上行。

### 两个我自己引入的 bug（都已修）

**bug 1：回调被自己的初始化清掉了 —— 表现为"喊醒了但没反应"**

`bot_wakeword_init()` 开头会 `memset(&s_w, 0, sizeof(s_w))`，而调用方是：

```c
bot_wakeword_set_cb(on_wake_word);   // 先把回调写进 s_w.cb
bot_wakeword_init();                 // 这里 memset 又把它清成 NULL
```

于是**回调被初始化抹掉了**。表现极具迷惑性：

* 固件**能**打印「唤醒词命中！」（说明模型、AFE、麦克风全都正常）；
* 但 `if (s_w.cb != NULL)` 永远为假 → 事件一个都发不出去；
* 从 PC 侧看就是"喊醒了，板子毫无反应"，很容易误判成检测失败。

→ 修法：在 memset **之前**把回调存到栈上，之后再恢复。

> 教训：**带 `memset` 的 init 与"先 set 后 init"的调用顺序是天然冲突的。**
> 更稳妥的设计是 init 里不整体清零，或让 set_cb 幂等可后置。

**bug 2：`WS_MAX_MSG` 太小，长音频会把设备踢下线**

音频走 base64（体积 ×4/3）。原上限 512KB 意味着**原始音频只能到 384KB**
（约 12 秒 16kHz 单声道），稍长的 TTS 回应就会被固件判为"消息过大，断开"，
表现为设备无故掉线。→ 提到 1MB，并在 PC 侧加了超限检查（给出可读错误
而不是打断连接）。彻底解法是音频分片传输，属协议扩展。

### 实机验证结果（端到端闭环打通）

用户对着板子说「Hi 小星」，固件与 PC 两侧同时验证：

**固件侧：**

```
bot_wake: —— 开始采集，唤醒检测已启用：请对板子清晰地说「Hi 小星」——
bot_wake: 唤醒词命中！（第 1 次，模型序号=1）
app: 唤醒词命中 → 上报 wake_word 事件
app: wake_word 事件上报 ok
```

**PC 侧（同一次触发，自动完成整轮）：**

```
device.event        {"event": "wake_word", "data": {"phrase": "Hi,小星"}}
speech.transcribed  {"text": "（离线识别：这里是一句话）", "duration_s": 10.44}
agent.user_message  {"text": "（离线识别：这里是一句话）"}
agent.reply         {"text": "我听到你说「…」。我是一台会看、会走、会做表情的小机器人。"}
```

即：**喊一声 → 本地唤醒 → 上报事件 → PC 自动采集 → 识别 → 决策 → 回复**，
全链路自动完成，无需手动点按钮。

### 怎么验证

```bash
# 一键测试（自己开串口 + 自动触发采集 + 明确报告端口占用/未命中原因）
cd D:\dsh\sparkbot\sparkbot-esp32
C:\Espressif\python_env\idf5.5_py3.13_env\Scripts\python.exe tools\wake_test.py COM15 25
```

> **串口同一时刻只能被一个进程打开。** 先用别的工具占着端口时，本工具会
> 报"打不开"，看起来也像"串口没打印"。项目自带的 `esp32gw.exe`
> 也会抢端口，必要时先 `taskkill /F /IM esp32gw.exe`。

采集期间串口会打印可诊断的状态行：

```
bot_wake: AFE 进度: feed=201 丢=0 size=1024 wakeup_state=0 vad=0
```

| 字段 | 含义 | 出问题时指向 |
|---|---|---|
| `feed` | 已喂入帧数 | 不涨 → 音频没进 AFE |
| `vad` | 是否检测到人声 | 说话时仍 0 → 麦克风没拾到（距离/增益） |
| `wakeup_state` | 唤醒词状态 | `1` = 命中 |

### 许可

`esp-sr` 用的是 **"ESPRESSIF MIT License"**：文本与 MIT 基本一致，
但开头多一句限定 **"for use on all ESPRESSIF SYSTEMS products"**。
本项目跑在 ESP32-S3 上，属于该范围；但它**不是通用 MIT**，
如果你要把固件移植到非乐鑫芯片，需要另行确认。唤醒词模型文件另有条款。

### 内存：FreeRTOS 任务栈默认放不进 8MB PSRAM

这块板子是 **ESP32-S3-N16R8**（16MB Flash + 8MB PSRAM），
但开了唤醒词后出现过这个失败：

```
E bot_audio: 创建采集任务失败（需要 8192 字节栈）
E bot_audio:   内部RAM可用: 17235 字节, 最大连续块: 7680 字节
E bot_audio:   PSRAM可用: 7641676 字节
```

**FreeRTOS 任务栈默认只能从内部 RAM 分配**，而 AFE 初始化会占掉大部分
内部 RAM，剩下最大连续块 7680 < 8192，于是 `xTaskCreate` 失败。
对外表现是 `start_listen` 报 "麦克风不可用"（hardware_fault）——
**症状像麦克风坏了，实际是内存放不下栈**。

→ 用 `xTaskCreateWithCaps(..., MALLOC_CAP_SPIRAM)` 把栈放到 PSRAM。

> ⚠️ 配套的坑：用 `WithCaps` 创建的任务**必须**用 `vTaskDeleteWithCaps()`
> 退出。用普通的 `vTaskDelete()` 会按内部 RAM 去释放 PSRAM 的栈，
> 导致堆损坏 —— 这类崩溃往往很久以后才暴露，极难定位。

### 方法论教训

1. **"设备每 40 秒重连" / "采集+播放会重启" 都是假象** ——
   由我用 `esptool ... run` 复位板子后再读串口的操作造成。
   用 uptime 这类**设备侧指标**判定是否重启才可靠。

2. **"任务表里没有 AFE 任务"也不能当作结论** ——
   `uxTaskGetSystemState()` 只报告启用 trace facility 后创建的任务，
   对预编译库里的任务不可靠。别拿它当"任务没创建"的证据。

3. **升级/对齐到已验证可用的版本，比逐参数试错快得多。**
   我在 1.9.5 上试了十几组配置组合都失败；
   换成参考工程同款的 2.4.7 + `afe_config_init()` 一次就通。

---

## 摄像头排查记录（未解决）

### 现象

```
I bot_power: 摄像头供电: 0x90=0x77  IO(ALDO2)=on@2800mV  AVDD(BLDO1)=on@2800mV  DVDD(BLDO2)=on@1200mV
D camera:    Using existing I2C port
D camera:    Searching for camera address
E camera:    Detected camera not supported.
E camera:    Camera probe failed with error 0x106(ESP_ERR_NOT_SUPPORTED)
```

### 已排除的原因

固件内置了 I2C 扫描（探测失败时自动执行），输出：

```
I bot_cam:   I2C 应答: 0x18     ← ES8311 音频 codec
I bot_cam:   I2C 应答: 0x34     ← AXP2101 电源管理
I bot_cam: 摄像头初始化失败后: I2C 总线共 2 个设备应答
```

由此可以确定：

| 怀疑点 | 结论 |
|---|---|
| I2C 总线不通 | ❌ 排除 —— codec 与 PMIC 都正常应答 |
| 摄像头供电没开 | ❌ 排除 —— 三路 LDO 已使能且电压回读正确 |
| SCCB 与音频抢 GPIO | ❌ 排除 —— 日志显示 `Using existing I2C port`，无 GPIO 冲突告警 |
| 组件版本 API 不兼容 | ❌ 排除 —— 已锁 `esp32-camera ==2.1.7`（用新 `i2c_master`） |
| **传感器本身没应答** | ✅ **就是这个** —— 0x30 不在总线上 |

### 下一步（需要动手检查硬件）

`0x106` 是 `sensor_probe()` 在**遍历完全部已知传感器地址**后仍无应答时返回的，
所以不是"型号不识别"，而是**总线上没有摄像头**。按可能性排序：

1. **摄像头模组/排线没插好** —— 最可能。重新拔插那个 FPC 排线，
   注意金手指方向和卡扣是否锁紧；
2. **模组本身故障** —— 有条件的话换一个 OV2640 模组试；
3. **PWDN 极性** —— 当前配置 `PWDN = GPIO48`。若模组的 PWDN 是反相
   （低有效）而固件按高有效处理，传感器会被一直压在掉电态。
   可以试把 menuconfig 里 `摄像头 → PWDN（-1 = 未接）` 改成 `-1`；
4. **XCLK 没到模组** —— 传感器需要外部时钟才会响应 SCCB。
   可以拿示波器量 GPIO8 是否有 20 MHz 方波。

排除顺序建议：**先重插排线** → 再看是否需要 `PWDN=-1` → 最后怀疑模组。

> 顺带说明：早期关于这块板的文档里记录摄像头**曾经工作过**
> （有正常抓帧的调参记录），也记录了"冷启动时只有一路 LDO 开"的坑。
> 说明硬件设计是通的，当前更像是连接或状态问题。

---

## 目录结构

```
sparkbot-esp32/
├── CMakeLists.txt          顶层工程
├── build.bat               构建/烧录辅助（绕过 export 校验）
├── partitions_16m.csv      16MB 分区表（4MB app + coredump）
├── sdkconfig.defaults      PSRAM / 控制台 / 协议栈默认配置
├── main/
│   ├── Kconfig.projbuild   全部可配置引脚与参数
│   ├── idf_component.yml   组件依赖（版本锁死）
│   ├── app_main.c          启动顺序与主循环
│   ├── bot_protocol.h      协议常量（与 PC 端 protocol.py 对应）
│   ├── bot_engine.c/.h     协议引擎：hello/遥测/RPC/事件/帧/音频
│   ├── bot_json.c/.h       cJSON 取值辅助（带类型校验）
│   ├── bot_net.c/.h        WiFi STA + WebSocket **客户端**
│   ├── bot_ws.c/.h         WebSocket **服务端**（RFC6455 子集，备用）
│   ├── bot_hw_i2c.c/.h     板载 I2C 总线（三设备共用）
│   ├── bot_hw_display.c/.h LCD + 表情绘制 + 脏矩形刷新
│   ├── bot_hw_audio.c/.h   I2S + ES8311 采集/播放/音调
│   ├── bot_hw_camera.c/.h  OV2640 抓帧/推流 + I2C 扫描诊断
│   ├── bot_hw_power.c/.h   AXP2101 电池遥测 + 摄像头供电
│   ├── bot_hw_motor.c/.h   双路 H 桥 + 差速 + 开环定时
│   └── bot_font.h          8x16 ASCII 字库（自动生成，勿手改）
└── tools/gen_font.py       字库生成脚本
```

### 为什么同时有 WebSocket 服务端和客户端

SparkBot 协议规定**板子主动连 PC**（PC 是服务端），
所以对接 PC 走的是 `bot_net.c` 的客户端路径。

`bot_ws.c`（服务端）是为另一种部署形态准备的：板子开热点、
PC 主动连过来。它也作为 RFC 6455 的完整参考实现保留
（含握手、掩码、分片重组）。当前主流程不使用它，但会一起编译。

---

## 协议实现说明

协议常量在 `bot_protocol.h`，与 PC 端 `sparkbot/device/protocol.py`
和 `docs/protocol.md` 一一对应。**三者必须同步修改。**

### 几个协议细节的处理

**一个音频采集会话只发一个 `end`**：这是 PC 端明确要求的 ——
多发一个 `end` 会让下一次采集刚开始就误判结束、拿到空音频。
实现上用 `ended` 标志确保正常结束与被 `stop_listen` 取消
两条路径加起来只发一次。

**先发 `frame` 再回 `result`**：PC 两种顺序都支持，但先发帧能让
PC 在收到帧时立即完成挂起的请求，省一次往返。

**未知动作必须回错误**：`unsupported_action` + 可读的 message。
静默丢弃会表现为"PC 侧卡到超时"，很难定位。

**开环定时自己停车**：`drive` 带 `duration_ms` 时固件到点自动停下并发
`motion_done`。不能等 PC 再发 `stop` —— 网络断了机器人会一直跑。
主循环检测到连接断开时也会立刻 `bot_motor_stop()`。

**固件侧自己做速度钳制**：`bot_motor_drive()` 里按 Kconfig 的
`max_linear` / `max_angular` 再钳一次。不能只依赖 PC 端护栏 ——
PC 可能是任意客户端，而这是会真的撞到东西的设备。

---

## 与 PC 端的配合

PC 端配置（`D:\dsh\sparkbot`）：

```bash
python run.py                    # 默认监听 0.0.0.0:8765，路径 /robot
```

板子连上后，PC 控制台会显示设备与遥测。可用的工具取决于固件上报的能力 ——
摄像头不通时 PC 端不会向模型暴露 `look_around`，
所以现在可以先验证语音、表情与遥测链路。

验证方式：

```bash
python tests/smoke.py                      # 全链路自检
python tests/smoke.py --face happy         # 只测屏幕
python tests/smoke.py --chat "你好"         # 走一轮对话
```

固件侧串口日志会同步打印收到的每条 `command`，便于对照。
