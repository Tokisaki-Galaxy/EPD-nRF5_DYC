# 看板固件与上位机

## 组成

| 位置 | 作用 |
|---|---|
| 固件分支 `dashboard` | 面向看板的精简固件，可 OTA，体积约 28KB |
| `host/epdash.py` | 差分、窗口、局刷、计时 |
| `dashboard/refresh.py` | 渲染加推屏的一体入口，供计划任务调用 |
| `dashboard/epd_update.py` | 抓数据并用 Edge 无头截图输出 800x480 PNG |

## 固件新增命令

| 命令 | 作用 |
|---|---|
| 0x39 | 触发 DRF，并在设备内部记录真实耗时，回传 `ms=数字` |
| 0x3A | 回传 `ready=0/1`，表示本次上电后面板缓冲是否还有效 |

0x39 的忙等改成先等 BUSY 拉低再等释放。原来的写法在 BUSY 尚未拉低时会立即返回，主机测到的时间不可信。

## 关键结论

### 断线必须保活

`on_disconnect` 原来调用 `drv->sleep`，也就是 POF 加 DSLP。手册写明寄存器数据只能保留到 VDD 断电或进入深睡，所以深睡会清掉 NEW/OLD 缓冲，下次只能全刷。

现在断开时只发 POF，并把 RST 保持高电平。POF 关掉升压但保留 RAM，VDD 一直在。

### 真局刷靠零波形

PSR 置 0x3F，进入黑白模式并从寄存器取波形。CDI 的 0xA9 打开 N2OCP，每次刷新结束把 NEW 拷进 OLD。

四张转移波形里，未变化的像素走 LUTWW 和 LUTKK。把这两张写全零，它们就完全不被驱动，这才是真正的局部刷新。

### 窗口只限制数据，不限制驱动

PTL 设置的是数据写入窗口，DRF 仍然扫描整屏。窗口外的像素走 W→W 或 K→K，零波形下没有动作，所以看起来像只有窗口在变。

一旦把 LUTWW 和 LUTKK 写成完整波形，窗口外也会被驱动，整屏都会闪。所以干净模式不能用于局刷。

### 设备侧可验证

读 FLG 寄存器 0x71，D6 是 PTL_Flag：

| 状态 | FLG | PTL |
|---|---|---|
| 普通模式 | 0x13 | 0 |
| PTIN 之后 | 0x53 | 1 |
| PTOUT 之后 | 0x13 | 0 |

### 帧时间

设备内计时显示整屏完整波形 2662 毫秒，约 124 帧，单帧约 20 毫秒。OTP 默认波形约 780 帧，对应 16.7 秒。

局刷和整屏耗时一样，因为耗时由波形帧数决定，和窗口大小无关。真正省下的是数据传输量和窗口外的闪烁。

## 用法

```powershell
python dashboard/refresh.py            # 抓数据、渲染、差分推屏
python dashboard/refresh.py --full     # 强制整屏
python host/epdash.py image.png        # 只推一张图
python host/epdash.py image.png --dry-run
```

状态保存在 `host/.epdash_state.npz`，记录上一帧和局刷计数。连续 12 次局刷后自动插一次整屏刷新，清除残影。

## 计划任务

每 30 分钟运行一次：

```
程序: python
参数: Z:\Code\GitHub\EPD-nRF5\dashboard\refresh.py
起始于: Z:\Code\GitHub\EPD-nRF5
```

## 已知限制

- 红色部分无法局刷，只有黑白能差分
- 大块实心填充会出现透底，因为未变化像素不被驱动，残留灰度会显出来。实测文字和数字更新完全看不出残留，三个价格同时改也没有残影
- 完整波形 2.66 秒，还能压到 1 秒左右，代价是驱动更弱、残影更明显
