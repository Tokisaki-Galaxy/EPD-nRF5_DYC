# -*- coding: utf-8 -*-
"""
refresh.py: 一次完整的看板刷新, 供计划任务调用.

    渲染 epd_update.py -> epd.png
    推屏 host/epdash3.py -> 墨水屏（三色）

日志写到 refresh.log, 每次运行追加, 便于排查.

用法:
    python refresh.py            # 正常刷新
    python refresh.py --full     # 强制整屏
    python refresh.py --render   # 只渲染不推屏
"""

import argparse
import asyncio
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
PNG = BASE / "epd.png"
LOG = BASE / "refresh.log"
BLE_CMD = "62750002-d828-918d-fb46-b6c11c675aec"


def log(msg):
    line = "%s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def run(cmd, timeout):
    log("运行 " + " ".join(str(x) for x in cmd))
    proc = subprocess.run(cmd, cwd=str(ROOT), timeout=timeout,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    if out:
        for line in out.splitlines():
            log("  " + line)
    if proc.returncode != 0:
        raise RuntimeError("返回码 %d" % proc.returncode)


def query_device_status():
    """连上设备读温度和电压, 写进 data.json 的 weather.device, 显示成 29°C/2.8V."""
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError:
        log("没有安装 bleak, 跳过设备状态")
        return

    async def probe():
        dev = None
        for _ in range(4):
            res = await BleakScanner.discover(timeout=6, return_adv=True)
            for _k, v in res.items():
                d, _a = v if isinstance(v, tuple) else (v, None)
                if "epd" in (d.name or "").lower():
                    dev = d
            if dev:
                break
            await asyncio.sleep(2)
        if dev is None:
            return None
        notes = []
        async with BleakClient(dev, timeout=25) as c:
            await c.start_notify(BLE_CMD, lambda _h, d: notes.append(bytes(d)))
            await c.write_gatt_char(BLE_CMD, bytes([0x37]), response=True)
            for _ in range(30):
                for n in notes:
                    if n.startswith(b"temp="):
                        return n.decode()
                await asyncio.sleep(0.1)
        return None

    try:
        text = asyncio.run(probe())
    except Exception as exc:
        log("设备状态读取失败: %s" % exc)
        return
    if text is None:
        log("没有读到设备状态, 沿用旧值")
        return

    fields = dict(p.split("=", 1) for p in text.split())
    temp = fields.get("temp", "?")
    try:
        volt = "%.1f" % (int(fields.get("mv", 0)) / 1000.0)
    except ValueError:
        volt = "?"
    if temp == "-99":
        log("温度读取失败, 沿用旧值")
        return
    status = "%s°C/%sV" % (temp, volt)

    path = BASE / "data.json"
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data.setdefault("weather", {})
    data["weather"]["device"] = status
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    log("设备状态 %s" % status)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--render", action="store_true")
    args = ap.parse_args()

    started = time.time()
    try:
        query_device_status()
        run([sys.executable, str(BASE / "epd_update.py"), str(PNG)], 180)
        if args.render:
            log("只渲染, 不推屏")
            return
        cmd = [sys.executable, str(ROOT / "host" / "epdash3.py"), str(PNG)]
        if args.full:
            cmd.append("--full")
        for attempt in (1, 2):
            try:
                run(cmd, 240)
                break
            except Exception as exc:
                if attempt == 2:
                    raise
                log("推屏失败, 15 秒后重试: %s" % exc)
                time.sleep(15)
        log("完成, 用时 %.1f 秒" % (time.time() - started))
    except Exception as exc:
        log("失败: %s" % exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
