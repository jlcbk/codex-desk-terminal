# HARDWARE.md — ESP32-S3-RLCD-4.2 引脚与硬件证据表

任务：P0.3（角色 A3 板级/功耗）。性质：**纯文档审计，无硬件在手、未烧录、未做任何硬件验证**。本文只回答"每个引脚/参数从哪来、可信到什么程度"，不声称任何实测结论。

- 审计日期：2026-09-10
- 结论状态标记：`confirmed`（本地官方 demo 源码 + 同板实战代码 + wiki 中至少两路独立来源一致）；`unverified`（仅有单一来源或推断，需原理图/实测）；`conflicting`（来源之间不一致，本文并列两说）
- 本地官方 demo = `/Users/cui/Documents/Projects/hermes-courier/vendor/waveshare-rlcd/`，为 Waveshare 官方仓库 `https://github.com/waveshareteam/ESP32-S3-RLCD-4.2` 的浅克隆，HEAD = `eb1f63427d735a22b9c30e22fa63ebddae1834d3`（commit 信息 "fix: update arduino Tools-Configuration image"）。以下路径均相对该目录，记作 `vendor/`。
- 实战代码 = `/Users/cui/Documents/Projects/hermes-courier/`（下记 `hermes/`）为**只读外部参考**：同板（编号 1301）真实跑过 Wi-Fi + ST7305 + ADC + RTC + 浅睡的固件，其结论仅作旁证，本项目不复用其代码。审计过程未写入该目录任何文件。

证据源优先级（按任务书）：本地官方 demo 源码 > hermes 实战代码 > Waveshare wiki > 官方 GitHub。wiki 抓取采用 Firecrawl（抓取日期 2026-09-10，页面以当日快照为准）。

---

## 1. 引脚表

官方 wiki GPIO 分配表来源：https://docs.waveshare.com/ESP32-ESPHome-Tutorials/Example-RLCD-Voice §2.2（下记 wiki-ESPHome 表）。

### 1.1 ST7305 显示 SPI（本项目核心，全部 confirmed）

| 信号 | GPIO | 来源 | 状态 |
|---|---|---|---|
| SPI MOSI | GPIO12 | `vendor/02_Example/ESP-IDF/09_LVGL_V9_Test/main/user_config.h:12`（`RLCD_MOSI_PIN GPIO_NUM_12`）；实例化 `09_LVGL_V9_Test/main/main.cpp:12`（`DisplayPort RlcdPort(12,11,5,40,41,…)`）；XiaoZhi 板级 `02_Example/XiaoZhi/XiaoZhiCode_V2.1.0/main/boards/waveshare-s3-rlcd-4.2/config.h:32`；hermes `firmware/courier_v01/config.h:25`；wiki-ESPHome 表 | confirmed |
| SPI SCLK | GPIO11 | 同上四路：`user_config.h:11`、`main.cpp:12`、XiaoZhi `config.h:31`、hermes `config.h:24`、wiki-ESPHome 表 | confirmed |
| CS | GPIO40 | `user_config.h:10`、`main.cpp:12`、XiaoZhi `config.h:30`、hermes `config.h:27`、wiki-ESPHome 表 | confirmed |
| DC | GPIO5 | `user_config.h:9`、`main.cpp:12`、XiaoZhi `config.h:29`、hermes `config.h:26`、wiki-ESPHome 表 | confirmed |
| RST | GPIO41 | `user_config.h:13`、`main.cpp:12`、XiaoZhi `config.h:33`、hermes `config.h:28`、wiki-ESPHome 表 | confirmed |
| TE（帧同步） | GPIO6 | `vendor/.../09_LVGL_V9_Test/main/user_config.h:14`（`RLCD_TE_PIN GPIO_NUM_6`）**仅定义**：`display_bsp.cpp` 全文无引用，hermes 驱动亦无引用，ESPHome wiki 配置也不用 TE | confirmed（引脚号）；**功能 unverified**（官方驱动从未接线使用，本项目也不得假设 TE 可用） |
| 忙检测（BUSY） | 无 | 官方 ESP-IDF `display_bsp.cpp` 无任何 busy 脚读取，写后不回读状态；wiki 亦无。ST7305 数据手册（https://files.waveshare.com/wiki/common/ST_7305_V0_2.pdf ）可能有 busy 机制，但**本板是否引出 unverified** | unverified（判定为"无 busy 脚"需原理图确认） |
| MISO | 未连接 | `display_bsp.cpp:19`（`buscfg.miso_io_num = -1`），写-only 总线 | confirmed |
| SPI 控制器 | SPI3_HOST | `display_bsp.h:46`（默认参数）、XiaoZhi `config.h:7` | confirmed |
| SPI 时钟 | 官方 IDF 例 10 MHz：`display_bsp.cpp:31`（`pclk_hz = 10 * 1000 * 1000`）；ESPHome wiki 建议 1 MHz；hermes 实战 24 MHz：`hermes/firmware/courier_v01/ST7305_U8g2.cpp:3`（真机跑通） | 三说不一：上限时钟未官方标定，P4.2 需自测上限 | conflicting |

### 1.2 按键

| 信号 | GPIO | 来源 | 状态 |
|---|---|---|---|
| BOOT 键 | GPIO0，低有效，内部上拉 | 官方厂例 `vendor/02_Example/ESP-IDF/10_FactoryProgram/components/port_bsp/button_bsp.c:12-14`（`BOOT_KEY_PIN 0`、`BOOT_Active 0`）与 :64-72（`GPIO_PULLUP_ENABLE`）；XiaoZhi `config.h:26`（`BOOT_BUTTON_GPIO GPIO_NUM_0`）；hermes `config.h:31` + `courier_v01.ino:1324`（`pinMode(BOOT_BTN_PIN, INPUT_PULLUP)`）；wiki-ESPHome 表 | confirmed |
| KEY 键（本项目页面切换键） | GPIO18，低有效，内部上拉 | **官方厂例** `button_bsp.c:17-19`（`GP18_KEY_PIN 18`、`GP18_Active 0`）+ :68-70（与 BOOT 同组上拉输入）；wiki-ESPHome 表（"KEY button (active low)"）。注意 hermes 代码不含 KEY（它复用 BOOT 键做 UI 键），故实战旁证缺失，但官方两路来源一致 | confirmed |

### 1.3 电源类

| 信号 | GPIO | 来源 | 状态 |
|---|---|---|---|
| BAT_ADC（电池分压采样） | GPIO4，外接 1/3 分压 | 官方 `vendor/02_Example/ESP-IDF/03_ADC_Test/components/port_bsp/adc_bsp.cpp:22`（ADC1 通道 3）+ :33（×3）；wiki-ESPHome 表（"Battery ADC, GPIO4, 3x voltage divider"）；hermes `config.h:33-35`。细节见 §2 | confirmed |
| PWR 键 | **无 GPIO 证据** | wiki 主页（https://docs.waveshare.com/ESP32-S3-RLCD-4.2 ）资源图注 05："PWR Button — Long press to power off, single click to power on"。全部本地代码（vendor + hermes）均无 PWR 引脚定义 | 行为 confirmed（wiki 单源）；**引脚号/固件可读性 unverified** |
| USB 插入检测 | 无 | hermes 调查（`hermes/firmware/courier_v01/board_status.h:446-456`，2026-09-09）：vendor 全树无 VBUS/充电检测代码，板 README 只把 "USB Type-C" 列为连接器；wiki 只描述 CHG 充电指示灯（充满熄灭，资源图注 14）。**固件不可读，本项目按"无 USB 检测"设计**（计划 §7.3 的"插线需按电源键"提示保持） | unverified（存在硬件检测电路的可能性由原理图排除/确认） |
| 充电管理 | — | wiki 主页产品简介"battery charge and discharge management circuit" + CHG/WRN 指示灯（WRN=电池反接常亮，资源图注 15）。充电 IC 型号未见于任何本地来源 | unverified（型号待原理图） |

### 1.4 I2C 总线（SDA=GPIO13 / SCL=GPIO14，confirmed）

| 器件 | 地址 | 来源 | 状态 |
|---|---|---|---|
| RTC PCF85063(A) | 0x51（7 位） | 官方 `vendor/02_Example/Arduino/04_I2C_PCF85063/04_I2C_PCF85063.ino:10`（`Rtc_Setup(&I2cbus, 0x51)`）；SensorLib `PCF85063Constants.h:39`（`PCF85063_SLAVE_ADDRESS = 0x51`）；厂例 10 `Rtc_Setup(&I2cbus,0x51)`（wiki ESP-IDF 页代码分析）；hermes H01 调查 `hermes/docs/planning/h01-rtc-investigation.md:28,42` | confirmed |
| SHTC3 温湿度 | 0x70 | wiki-ESPHome §5.1/5.2（总线扫描应见 0x18/0x40或0x42/0x70）；hermes `board_status.h` BOARD_ENV_I2C_ADDR | confirmed |
| ES8311 音频 DAC | 默认地址（扫描见 0x18） | XiaoZhi `config.h:23`；wiki-ESPHome §5.1 | confirmed（本项目不用，仅记录） |
| ES7210 音频 ADC | 默认地址（扫描见 0x40 或 0x42） | XiaoZhi `config.h:24`；wiki-ESPHome §5.1 | confirmed（本项目不用，仅记录） |
| PCF85063 中断/唤醒脚 INT | **未定义** | hermes H01 调查 `h01-rtc-investigation.md:65`：vendor 三个 user_config.h 均无 PCF85063 INT 定义（唯一 `PCF85063_INT 21` 出现在 XiaoZhi 工程的 LILYGO T-Display-S3-Pro 板，**不适用本板，禁止引用**）。官方 ESPHome wiki 也不用 RTC | unverified —— **RTC 闹钟唤醒路径在本板无任何证据**，P5.3 配置唤醒源前必须先查原理图/实测 |
| I2C 速率 | RTC 300 kHz、SHTC3 400 kHz（官方厂例按设备级配置） | `h01-rtc-investigation.md:63`（引 `i2c_equipment.cpp:204,14`） | confirmed |

I2C 引脚本身：SDA=13 / SCL=14，四路一致（`vendor/.../09_LVGL_V9_Test/main/user_config.h:17-18`、Arduino 例 `04_I2C_PCF85063/user_config.h:16-17`、XiaoZhi `config.h:21-22`、hermes `board_status.h:60-61`、wiki-ESPHome 表）。confirmed。
注意构造顺序陷阱（hermes `board_status.cpp:28-32`）：官方 Arduino 例 `I2cMasterBus I2cbus(14, 13, 0)` 是 (scl, sda, port)，SensorLib `SensorPCF85063::begin` 是 (Wire, sda, scl)——移植 ESP-IDF 时以 IDF API 参数为准，勿混用。

### 1.5 音频 I2S（本项目不用，记录备用；confirmed）

| 信号 | GPIO | 来源 |
|---|---|---|
| I2S MCLK | GPIO16 | XiaoZhi `config.h:14`；wiki-ESPHome 表 |
| I2S WS/LRCLK | GPIO45 | XiaoZhi `config.h:15`；wiki-ESPHome 表 |
| I2S BCLK | GPIO9 | XiaoZhi `config.h:16`；wiki-ESPHome 表 |
| I2S DIN（麦克风） | GPIO10 | XiaoZhi `config.h:17`；wiki-ESPHome 表 |
| I2S DOUT（扬声器） | GPIO8 | XiaoZhi `config.h:18`；wiki-ESPHome 表 |
| 功放使能 PA | GPIO46（高使能） | XiaoZhi `config.h:20`（`AUDIO_CODEC_PA_PIN GPIO_NUM_46`）；wiki-ESPHome §2.2 注（拉高才有输出） |
| 双麦克风阵列 + ES7210 回采 | — | wiki 主页资源图注 02/13 |

### 1.6 其他外设

| 外设 | 引脚/参数 | 来源 | 状态 |
|---|---|---|---|
| TF 卡（SDMMC 1-bit） | CLK=GPIO38、CMD=GPIO21、D0=GPIO39 | 官方 `vendor/02_Example/ESP-IDF/10_FactoryProgram/components/port_bsp/sdcard_bsp.h:15`（默认参数 `clk=38, cmd=21, d0=39, width=1`） | confirmed（本项目不用） |
| 主电池 | 18650 电池座 | wiki 主页资源图注 12；vendor `README.md:5` | confirmed |
| RTC 备用电池座 | PH1.0，仅支持可充电 RTC 电池 | wiki 主页资源图注 10 | confirmed（wiki 单源，板上实物待到货核对） |
| 扩展口 | 2×8 排母，2.54 mm | wiki 主页资源图注 11；wiki-ESPHome 表 | confirmed |
| 模组 | ESP32-S3-WROOM-1-N16R8（240 MHz 双核，16 MB Flash，8 MB Octal PSRAM，Wi-Fi + BLE5） | vendor `README.md:5`；wiki 主页资源图注 01；PSRAM octal/80 MHz 为官方 ESPHome 基础配置（wiki-ESPHome §3） | confirmed |

---

## 2. ADC 细节（GPIO4 电池采样）

### 2.1 GPIO4 → ADC 单元/通道映射：**confirmed**

官方采样代码（`vendor/02_Example/ESP-IDF/03_ADC_Test/components/port_bsp/adc_bsp.cpp`）逐行引用：

```cpp
// adc_bsp.cpp:9-23  Adc_PortInit：单元与通道配置
cali_config.unit_id = ADC_UNIT_1;            // :11  ADC 单元 1
cali_config.atten   = ADC_ATTEN_DB_12;       // :12  12 dB 衰减
cali_config.bitwidth= ADC_BITWIDTH_12;       // :13  12 位
adc_oneshot_config_channel(adc1_handle, ADC_CHANNEL_3, &config);  // :22  ADC1 通道 3

// adc_bsp.cpp:25-39  读电压：校准 mV × 3 还原分压
err = adc_oneshot_read(adc1_handle, ADC_CHANNEL_3, &value);      // :30
adc_cali_raw_to_voltage(cali_handle, value, &tage);              // :32  曲线拟合校准
vol = 0.001 * tage * 3;                                          // :33  三倍分压还原（V）
```

- ESP32-S3 芯片级事实：GPIO4 即 ADC1_CH3（ESP-IDF ESP32-S3 外设能力；官方代码"ADC1+通道3"与本板唯一模拟输入 GPIO4 组合自洽）。三路来源（官方代码、wiki-ESPHome 表、hermes `config.h:33-35` 注释"GPIO4 … (ADC1_CH3)"）一致 → confirmed。
- 校准方案：`adc_cali_create_scheme_curve_fitting`（:10-14），12 位 + 12 dB。分压后满电约 1.4 V（wiki-ESPHome §5.3 说明），落在 12 dB 量程（约至 3.1 V）内。
- 高阻分压的稳定时间/采样误差：**无任何来源给出数据，unverified**，按计划 §7.1 由 P4.4 实测。

### 2.2 官方电量映射（仅记录，**本项目不采纳**）

| 来源 | 下限 | 上限 | 映射 |
|---|---|---|---|
| 官方 ESP-IDF 例 `adc_bsp.cpp:41-51` `Adc_GetBatteryLevel` | <3.0 V → 0% | >4.12 V → 100% | 线性 `((vol-3.0)/1.12)*100` |
| wiki-ESPHome §5.3 YAML | `calibrate_linear 2.5 -> 0.0` | `4.2 -> 100.0` | 线性 + clamp 0–100；ADC 端 `multiply: 3.0`、`attenuation: 12db` |
| hermes 实战（同官方 IDF 例） | 3000 mV | 4120 mV | `board_status.h:479-481`；另加 "<2500 mV 视为 USB 供电"（:484） |

两说下限不同（3.0 V vs 2.5 V）→ conflicting，均**只作记录**。本项目按开发计划 §2.2/§7.1 使用 **3.600 V = 0%、4200 mV = 100%** 的本地策略，官方 2.5 V/3.0 V 下限不采纳；校准增益/偏置参数保留（P4.4 表计定标）。

---

## 3. 显示细节（ST7305）

### 3.1 原生分辨率与横屏实现

- 面板：4.2" 反射式单色 LCD，ST7305 控制器。wiki 主页写"resolution of 300 × 400"，wiki-ESPHome 外设表写"400×300"——这是同一面板两种朝向命名，**非冲突**：控制器原生按 300×400（竖）寻址。
- 官方 ESP-IDF 例的原生寻址形态（`display_bsp.cpp`）：
  - 帧缓冲 `DisplayLen = width*height/8`：400×300 横屏下为 **15000 字节（= 计划 §10 的整帧单色缓冲）**，`display_bsp.cpp:49`；
  - init 序列含 `0x36=0x48`（:152-153）、`0x3A=0x11`（:155-156），窗口 `2A: 0x12..0x2A`（列，:166-168、:191-193）+ `2B: 0x00..0xC7`（页，:170-172、:195-197），整帧一次 `2C` 写出（:199-201）；
  - 位打包（竖屏 `RLCD_SetPortraitPixel`）：每字节 4×2 像素块，`bit = 7 - ((local_x<<1)|local_y)`（:246、:262）；
  - **横屏不是控制器命令切换，而是软件映射**：`width==400` 时建 `InitLandscapeLUT`（:58-62、:334-355），对 `inv_y = height-1-y` 做 1×4 像素块打包、`bit = 7 - ((local_y<<1)|local_x)`（:349）；`AlgorithmOptimization=3`（查表法）为厂例实际编译形态（`display_bsp.h:10`）。
- hermes 实战独立印证：其 U8g2 驱动原生声明 `pixel_width=300, pixel_height=400`（`hermes/firmware/courier_v01/ST7305_U8g2.cpp:28-29`），用 **U8G2_R1 旋转**得到 400×300 横屏（`ST7305_U8g2.h` begin/注释；`PROJECT-STATUS.md:18` "300×400 竖装横用"），真机显示正确（hermes 33 卡验收 + Q01 六态走查，`hermes/PROJECT-STATUS.md:25-28`）。
- 结论（供 A3 P4.2）：**400×300 横屏逻辑帧需软件重排到 300×400 原生帧；位序为 MSB-first 打包**。官方 IDF 驱动路径：`vendor/02_Example/ESP-IDF/09_LVGL_V9_Test/components/port_bsp/display_bsp.{h,cpp}`；官方 U8g2 移植：`vendor/02_Example/Arduino/10_U8G2_Test/ST7305_U8g2.h`（hermes 固件即按此例派生，`hermes/firmware/courier_v01/config.h:23`）。
- LVGL 侧官方接法：RGB565 全帧双缓冲在 PSRAM（`lvgl_bsp.cpp:60-68`），flush 回调逐像素阈值转单色（`main.cpp:14-28`，`*buffer < 0x7fff → 黑`）。本项目单色管线按计划另设计，不照搬 RGB565 中转。

### 3.2 刷新能力参考（非承诺）

- 官方 ESP-IDF 例 SPI 10 MHz；wiki U8G2 例称"FPS 可达 74"（wiki ESP-IDF 页 11_U8G2_Test tip）；hermes 实战 24 MHz 跑通局部刷新（R02 脏带方案）。以上均为参考值，P4.2/P4.3 自行实测，本文不做结论。

---

## 4. 电源与唤醒

### 4.1 PWR 键（软开关）

- wiki 主页资源图注 05："长按关机、单击开机"——是接在电源管理电路上的**软开关**，不是机械自锁。长按断电后整板失电，屏幕内容不再有 MCU 维持。
- PWR 键是否有 GPIO 可读、深睡下能否作为唤醒源：**unverified**（无引脚号、无代码证据；待原理图）。保守设计：深睡后恢复路径按"PWR 重新上电（冷启动）"处理，与计划 §7.3 DEEP_SLEEP 行一致。

### 4.2 KEY / BOOT 作为深睡唤醒源

- 芯片能力：ESP32-S3 的 RTC 域 GPIO 为 GPIO0–21，均支持 ext0/ext1 深睡唤醒（ESP-IDF sleep 文档/芯片手册）。BOOT=GPIO0、KEY=GPIO18 都在该范围内，**理论上可做深睡唤醒源**。
- 本板实证情况：**没有任何本地或官方代码演示过这块板的深睡唤醒**。
  - hermes 只做过浅睡（light sleep）：`courier_v01.ino:1180-1192` `esp_sleep_enable_timer_wakeup()` + `esp_sleep_enable_gpio_wakeup()` + `gpio_wakeup_enable(GPIO0, GPIO_INTR_LOW_LEVEL)`；真机"进入/退出/BOOT 唤醒 ✓（2026-09-10）"见 `hermes/PROJECT-STATUS.md:29`。注意这是**浅睡 GPIO 唤醒 API**，不能证明深睡 ext0/ext1。
  - hermes 全程明确禁做深睡（负向 grep 红线，`hermes/reports/tasks/P02.md:81`、`P03.md:100,112`、`H02.md:95`）。
  - 官方 vendor 仓库 11 个示例无一涉及 sleep/wakeup。
- 回答：**KEY(GPIO18) 做深睡唤醒源 = unverified**（引脚 confirmed、低有效 confirmed、RTC GPIO 范围芯片手册支持，但本板无任何实测/代码先例）。P5.3 配置 `esp_sleep_enable_ext0/ext1_wakeup` 前必须真机验证（P5.4）；验证前的唯一保底恢复路径是 PWR 重新上电。计划 §7.3 "若 KEY 不是可用 RTC 唤醒脚，P0.3 必须确定真实替代路径"——本审计结论：**保底路径 = PWR 上电冷启动；KEY 深睡唤醒待 P5.4 实测定论**。
- USB 插入能否唤醒深睡：**unverified**（连 USB 检测本身都未证实，§1.3）。

### 4.3 深睡实测经验

hermes 无深睡实测（见上）；其浅睡经验仅两点可迁移，均属软件层：UART 排空后再睡（`courier_v01.ino:1184`）、`ESP_ERR_SLEEP_REJECT`（按键按住/射频忙）按普通循环重试处理（:1178-1179）。整板深睡电流无任何数据，P5 Gate 的 ≤100 µA 目标保持"未验证"。

---

## 5. 板卡修订

- SKU：33298（ESP32-S3-RLCD-4.2）、33507（ESP32-S3-RLCD-4.2-EN）（wiki 主页 SKU 表）。**本地 vendor 仓库与 wiki 均未标注 PCB 修订号（如 V1.x）**，unverified；P4.1 上电日志按计划要求输出实测板卡信息。
- 原理图 PDF 位置（**未解析内容**，P4.x 需要时可下载核对 INT/USB/PWR 疑点）：
  https://files.waveshare.com/wiki/ESP32-S3-RLCD-4.2/ESP32-S3-RLCD-4.2-schematic.pdf
  （来源：https://docs.waveshare.com/ESP32-S3-RLCD-4.2/Resources-And-Documents §1；同页另有 3D 结构图 rar、ST7305/ES8311/PCF85063/SHTC3 数据手册链接）
- 官方 GitHub：https://github.com/waveshareteam/ESP32-S3-RLCD-4.2 （本地 vendor 即其克隆，HEAD `eb1f634`）。

---

## 6. 厂商工具链版本要求（供 A0 锁版本）

| 项 | 要求 | 来源 | 状态 |
|---|---|---|---|
| **ESP-IDF** | wiki 明文："For the ESP32-S3-RLCD-4.2 development board, ESP-IDF version **V5.5.0 or above** is required."，安装截图用 V5.5.2 | https://docs.waveshare.com/ESP32-S3-RLCD-4.2/ESP-IDF "Setting Up the Development Environment" | confirmed（wiki 单源要求） |
| ESP-IDF（vendor 例自带 sdkconfig） | `09_LVGL_V9_Test/sdkconfig` 文件头："Espressif IoT Development Framework (ESP-IDF) **5.4.3** Project Configuration" | 本地 vendor 仓库 | **conflicting**：随仓库分发配置由 5.4.3 生成，而 wiki 当前要求 ≥5.5.0。可能 wiki 是后更新要求。**建议 A0 锁 5.5.x（如 5.5.2）**，并在 P4.1 用锁定版本重建官方最小例验证 |
| ESP-IDF manifest 下限 | 各例 `idf_component.yml`：`idf: version '>=4.1.0'`（泛化下限，无约束力）；`09_LVGL_V9_Test/main/idf_component.yml` 另引 `lvgl/lvgl: ^9.4.0` | 本地 vendor | confirmed（文件内容）；注意 wiki 例表称 09 例用 "LVGL V9.3.0" 而 manifest 写 `^9.4.0`，minor 级 conflicting，与本项目锁 LVGL 9.3.0 的决策无关（本项目自管 LVGL 版本） |
| Arduino 路线（参考） | hermes 真机验证组合：esp32 Arduino core **3.3.11**，fqbn `esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi,CDCOnBoot=cdc,PartitionScheme=app3M_fat9M_16MB`；官方 Arduino IDE 配置图 `vendor/Tools-Configuration.png` | `hermes/PROJECT-STATUS.md:51-55`；vendor 仓库 | confirmed（hermes 实测可编译烧录）；本项目主路线为 ESP-IDF，此行仅供回退参考 |

---

## 7. 冲突与不确定项汇总（不断言，待证）

| # | 项 | 两说/缺口 | 处置建议 |
|---|---|---|---|
| 1 | IDF 版本 | wiki ≥5.5.0 vs vendor sdkconfig 5.4.3 生成 | A0 锁 5.5.x，P4.1 复现官方例验证 |
| 2 | 官方电量下限 | IDF 例 3.0 V vs ESPHome 例 2.5 V | 均不采纳；本项目 3.600 V 策略（计划 §7） |
| 3 | SPI 时钟上限 | 10 MHz（官方 IDF）/ 1 MHz（ESPHome wiki）/ 24 MHz（hermes 实测跑通） | P4.2 自测上限，从保守值爬升 |
| 4 | RTC 型号命名 | README/数据手册链 "PCF85063" vs ESPHome 表 "PCF85063A"；SensorLib 驱动按 85063 鉴别 | 以原理图/芯片丝印为准；驱动按 SensorLib 0x51 路径 |
| 5 | KEY 深睡唤醒 | 引脚 confirmed，深睡可用性零证据 | P5.4 实测；保底 PWR 上电 |
| 6 | USB 检测 / USB 唤醒 | 无任何固件可读证据 | 按"无检测"设计；提示插线后需按 PWR |
| 7 | PWR 键引脚/可读性 | 仅 wiki 行为描述 | 原理图核对；不写"理论唤醒"代码 |
| 8 | PCF85063 INT / 闹钟唤醒 | 本板无 INT 引脚定义 | 不使用 RTC 唤醒；如需，先查原理图 |
| 9 | PCB 修订号 | 无来源 | P4.1 启动日志实测记录 |
| 10 | TE/busy | GPIO6 定义但官方驱动从未使用；无 busy 脚证据 | 驱动不做 TE 依赖；写后延时兜底 |

## 8. 复核命令

```sh
# 官方引脚定义（显示/I2C）
grep -n "RLCD_\|ESP32_I2C" /Users/cui/Documents/Projects/hermes-courier/vendor/waveshare-rlcd/02_Example/ESP-IDF/09_LVGL_V9_Test/main/user_config.h
# 官方显示实例化与横屏映射
grep -n "DisplayPort RlcdPort\|InitLandscapeLUT\|DisplayLen" /Users/cui/Documents/Projects/hermes-courier/vendor/waveshare-rlcd/02_Example/ESP-IDF/09_LVGL_V9_Test/main/main.cpp /Users/cui/Documents/Projects/hermes-courier/vendor/waveshare-rlcd/02_Example/ESP-IDF/09_LVGL_V9_Test/components/port_bsp/display_bsp.cpp
# 官方 ADC（GPIO4→ADC1_CH3、×3、3.0/4.12V 映射）
sed -n '9,51p' /Users/cui/Documents/Projects/hermes-courier/vendor/waveshare-rlcd/02_Example/ESP-IDF/03_ADC_Test/components/port_bsp/adc_bsp.cpp
# 官方按键（BOOT=0 / KEY=18，低有效上拉）
grep -n "KEY_PIN\|Active\|PULLUP" /Users/cui/Documents/Projects/hermes-courier/vendor/waveshare-rlcd/02_Example/ESP-IDF/10_FactoryProgram/components/port_bsp/button_bsp.c
# 官方 TF 卡引脚
grep -n "CustomSDPort(" /Users/cui/Documents/Projects/hermes-courier/vendor/waveshare-rlcd/02_Example/ESP-IDF/10_FactoryProgram/components/port_bsp/sdcard_bsp.h
# 官方音频引脚
grep -n "GPIO" /Users/cui/Documents/Projects/hermes-courier/vendor/waveshare-rlcd/02_Example/XiaoZhi/XiaoZhiCode_V2.1.0/main/boards/waveshare-s3-rlcd-4.2/config.h
# IDF 版本证据（wiki 5.5.0+ vs 本地 sdkconfig 5.4.3）
head -5 /Users/cui/Documents/Projects/hermes-courier/vendor/waveshare-rlcd/02_Example/ESP-IDF/09_LVGL_V9_Test/sdkconfig
grep -rn "version" /Users/cui/Documents/Projects/hermes-courier/vendor/waveshare-rlcd/02_Example/ESP-IDF/09_LVGL_V9_Test/main/idf_component.yml
# hermes 实战侧（只读）：电池引脚、浅睡唤醒、深睡禁令
grep -n "BATT_ADC_PIN\|BOOT_BTN_PIN" /Users/cui/Documents/Projects/hermes-courier/firmware/courier_v01/config.h
sed -n '1180,1192p' /Users/cui/Documents/Projects/hermes-courier/firmware/courier_v01/courier_v01.ino
grep -rn "esp_sleep_enable_ext\|esp_deep_sleep" /Users/cui/Documents/Projects/hermes-courier/firmware/  # 预期：无命中
# vendor 仓库指纹
cd /Users/cui/Documents/Projects/hermes-courier/vendor/waveshare-rlcd && git rev-parse HEAD && git remote get-url origin
```
