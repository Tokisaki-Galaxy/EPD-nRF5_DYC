# 项目文档

本文记录这个 fork 的结构、协议、构建方式与已知问题，面向后续维护。

## 版本关系

```
tsl0922/EPD-nRF5          原始项目，主线
└── YCD12/EPD-nRF5_DYC    设备实际在跑的固件，Tmp 分支，版本 0x20
    └── 本仓库            在 YCD12 基础上加修复与 CI
```

本仓库是 YCD12 的 fork，保留完整历史，可以直接向 YCD12 提 PR。

## 硬件

| 项目 | 值 |
|---|---|
| MCU | nRF52811，192KB flash，24KB RAM |
| 协议栈 | S112 7.3.0，起始 0x1000 |
| 面板 | UC8179，800x480，黑白红，型号 ID 0x07 |
| 供电 | 4 颗 CR2450 并联 |

## 内存地图

| 地址 | 内容 | 大小 |
|---|---|---|
| 0x00000 | MBR | 4KB |
| 0x01000 | S112 协议栈 | 到 0x18FFF |
| 0x19000 | 应用 | 0x10000，64KB |
| 0x29000 | DFU 引导 | 约 24KB |
| 0x2F000 附近 | 引导设置与 FDS 页 | 数 KB |
| 0x10001014 | UICR 中的引导起始地址 | 4 字节 |

应用区上限 64KB，这是本仓库所有构建配置的核心约束。

注意：上游的 Keil 工程和 SDK 链接脚本都写的是 `0x17000`，那是「没有引导」时的布局。本仓库统一改为 `0x10000`，避免静默覆盖引导。

## 固件结构

| 文件 | 职责 |
|---|---|
| `main.c` | 启动、蓝牙栈、DFU 注册、看门狗、休眠 |
| `EPD/EPD_service.c` | 自定义 BLE 服务、命令解析、图像传输 |
| `EPD/EPD_driver.c` | 引脚、SPI、复位、忙等待、电压采样 |
| `EPD/UC81xx.c` | UC8159/UC8176/UC8179/JD 系列驱动 |
| `EPD/SSD16xx.c` | SSD1619/SSD1677 驱动 |
| `EPD/EPD_config.c` | 基于 FDS 的配置持久化 |
| `GUI/` | 分带渲染、农历、字体、Adafruit_GFX 移植 |
| `html/` | 网页端，块传输与抖动 |

### 渲染方式

屏幕整帧需要 48000 字节，而 nRF51 只有 16KB RAM、nRF52 也只有 24KB，放不下。GUI 采用分带渲染：按可用堆算出每条带的高度，画完一条通过回调直接写入屏幕 RAM：

```c
int16_t ph = (__HEAP_SIZE - 512) / (data->width / 8);
```

每带会重新走一遍完整布局，这是已知的性能问题，优化方向是按可见行区间裁剪。

### 配置持久化

`epd_config_t` 共 13 字节，字段顺序：

```
mosi, sclk, cs, dc, rst, busy, bs, model_id,
wakeup_pin, led_pin, en_pin, display_mode, week_start
```

订阅命令特征值后，固件会立刻把这块内存原样推送出来。网页据此回填引脚和型号。

## BLE 协议

### 服务

| 用途 | UUID |
|---|---|
| 服务 | `62750001-d828-918d-fb46-b6c11c675aec` |
| 命令与通知 | `62750002-d828-918d-fb46-b6c11c675aec` |
| 应用版本 | `62750003-d828-918d-fb46-b6c11c675aec` |
| 安全 DFU | `0000fe59-...` |

### 命令

| 值 | 命令 | 说明 |
|---|---|---|
| 0x00 | SET_PINS | 设置引脚映射 |
| 0x01 | INIT | 初始化屏幕，回发 mtu 与时间 |
| 0x02 | CLEAR | 填白并刷新 |
| 0x03 | SEND_COMMAND | 透传单条屏幕命令 |
| 0x04 | SEND_DATA | 透传数据 |
| 0x05 | REFRESH | 刷新 |
| 0x06 | SLEEP | 屏幕休眠 |
| 0x20 | SET_TIME | 时间戳加时区加模式 |
| 0x21 | SET_WEEK_START | 每周起始日 |
| 0x30 | WRITE_IMAGE | 老式整段写 RAM |
| 0x31 | WRITE_BLOCK | 带 CRC 的块传输 |
| 0x32 | QUERY_STATUS | 查询接收位图 |
| 0x33 | RESET_TRANSFER | 复位传输状态 |
| 0x90 | SET_CONFIG | 整体写入配置 |
| 0x91 | SYS_RESET | MCU 复位 |
| 0x92 | SYS_SLEEP | MCU 休眠 |
| 0x99 | CFG_ERASE | 擦除配置 |

### 块传输

```
[0x31][块号低][块号高][总数低][总数高][cfg][载荷][CRC低][CRC高]
```

- 载荷最大 244 减 8，即 236 字节
- CRC 为 CRC16/X25，多项式 0x8408，初值 0xFFFF，只覆盖载荷
- cfg 低半字节 0x0F 表示黑白层，0x00 表示红色层
- cfg 高半字节 0x00 表示该层第一块，会触发 DTM 命令与窗口设置
- 每块最多 244 字节 ATT 值，需要 MTU 247

状态查询 `0x32` 返回：

```
[0xA1][总数低][总数高][已收低][已收高][会话][活动标志][位图...]
```

块确认通知为 `[0xA0][块号低][块号高][状态]`，状态 0 表示成功。

### 屏幕写入的关键约束

每个数据层开始前必须重设 RAM 窗口，否则地址计数器停在上层末尾，整个平面会偏移。这一点在 `UC81xx_Write_Ram` 中实现，也是 PR #4 修的问题。

## 构建

### Keil

仓库保留 `Keil/EPD-nRF51.uvprojx` 和 `Keil/EPD-nRF52.uvprojx`，但工程里的应用区大小仍是 0x17000，编译前必须改成 0x10000。

### GCC 与 CI

`Makefile.nRF52` 是 GCC 构建入口。CI 使用的流程：

```bash
cp Makefile.posix SDK/17.1.0_ddde560/components/toolchain/gcc/Makefile.posix
cp Makefile.nRF52 Makefile
make -j4
```

`Makefile.posix` 指定工具链根为 `/usr/bin/`。SDK 的构建规则要求项目目录下存在名为 `Makefile` 的文件。

链接脚本 `gcc_nrf52_64k.ld` 把应用区限制在 0x10000，超出直接链接失败。

打包：

```bash
nrfutil pkg generate \
  --hw-version 52 --sd-req 0x126 --sd-id 0x126 \
  --application _build/nrf52811_xxaa.hex \
  --application-version 0x22 \
  --key-file tools/priv.pem out.zip
```

### 体积预算

| 构建 | text | data | 合计 | 余量 |
|---|---|---|---|---|
| fork 加修复 | 64504 | 540 | 65044 | 492 |
| 主线 | 64860 | 316 | 65176 | 360 |

余量很小，新增功能前先看体积。

为了装下，做了这些取舍：

- 去掉 GUI 里的双精度乘法，改整数
- 电压改为整数毫伏，避免浮点打印
- 开启 `-flto`
- 关闭异常展开表，定义 `NDEBUG`
- 移除未使用的日志与 RTT 源文件

## 刷机

设备使用 Nordic 安全 DFU，无按键入口，特征值 `8ec90003`。

进入 DFU 的方式：

1. 连接设备，对 `8ec90003` 开启指示
2. 写入 `0x01`
3. 设备返回 `20 01 01` 后复位，广播为 `DfuTarg`

手机端用 nRF Connect Device Manager，选择 zip 包上传。

### 版本规则

OTA 包的版本必须高于设备当前应用版本。当前设备是 0x20，因此：

- 第一步包 0x21
- 主线包 0x22

签名不对或版本不够，引导会拒绝，不会写入 flash。

## 已知问题

| 问题 | 影响 | 状态 |
|---|---|---|
| 写 RAM 未复位窗口 | 三色图红层错位 | PR #4 |
| 硬币电池供电不足 | 刷新出现纵向渐变 | 换电池或改常电 |
| Keil 与链接脚本的应用区为 0x17000 | 可能覆盖引导 | 本仓库改为 0x10000 |
| GUI 每带重排整幅 | 刷新慢 | 未处理 |
| 时钟模式每分钟整刷 | 残影 | 固件行为 |

## 分支约定

| 分支 | 用途 |
|---|---|
| `Tmp` | 上游 YCD12 的默认分支 |
| `fix/ram-window` | 只含窗口修复，PR #4 使用 |
| `build/step1` | fork 加修复的可刷固件，版本 0x21 |
| `mainline` | tsl0922 主线的可刷固件，版本 0x22 |

上游更新后，用 `git fetch upstream` 再 rebase 到 `upstream/Tmp`。三个构建分支只保存构建适配，不叠加功能改动，避免和上游冲突。
