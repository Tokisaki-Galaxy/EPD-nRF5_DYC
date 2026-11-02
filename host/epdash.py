# -*- coding: utf-8 -*-
"""
epdash.py: 把一张 800x480 的 PNG 推到墨水屏, 只刷新变化的区域.

用法:
    python epdash.py epd.png              # 差分更新
    python epdash.py epd.png --full       # 强制整屏刷新
    python epdash.py epd.png --dry-run    # 只算差分框, 不连蓝牙

策略:
    首次运行或者设备报告 ready=0 时做整屏刷新.
    之后每次只推变化的包围盒.
    连续 N 次局刷后插一次整屏刷新清残影, 默认 12 次.

状态文件放在脚本同目录的 .epdash_state.npz, 保存上一帧和计数.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from bleak import BleakClient, BleakScanner

CMD = "62750002-d828-918d-fb46-b6c11c675aec"
W, H = 800, 480
ROW = W // 8
BLOCK = 8          # 差分最小粒度
FULL_EVERY = 12

BASE = Path(__file__).resolve().parent
STATE = BASE / ".epdash_state.npz"

VCOM = [0x00, 0x0F, 0x0F, 0x00, 0x00, 0x01,
        0x00, 0x0F, 0x01, 0x0F, 0x01, 0x02,
        0x00, 0x0F, 0x0F, 0x00, 0x00, 0x01] + [0] * 24
WW = [0x10, 0x0F, 0x0F, 0x00, 0x00, 0x01,
      0x84, 0x0F, 0x01, 0x0F, 0x01, 0x02,
      0x20, 0x0F, 0x0F, 0x00, 0x00, 0x01] + [0] * 24
BW = list(WW)
WB = [0x80, 0x0F, 0x0F, 0x00, 0x00, 0x01,
      0x84, 0x0F, 0x01, 0x0F, 0x01, 0x02,
      0x40, 0x0F, 0x0F, 0x00, 0x00, 0x01] + [0] * 24
BB = list(WB)


def pkt(cmd, payload=b""):
    return bytes([cmd]) + bytes(payload)


def load_frame(path):
    """读 PNG, 返回 (H, ROW) 的 uint8, 每字节 8 像素, bit=1 白, bit=0 黑."""
    img = Image.open(path).convert("L").resize((W, H), Image.LANCZOS)
    arr = np.asarray(img, dtype=np.uint8)
    bits = np.where(arr < 128, 0, 1).astype(np.uint8)
    return np.packbits(bits, axis=1)


def diff_box(old, new):
    """返回变化区域的 x0,y0,x1,y1, 已按 8 像素对齐, 无变化返回 None."""
    changed = old != new
    rows = np.flatnonzero(changed.any(axis=1))
    if rows.size == 0:
        return None
    y0 = int(rows[0]) // BLOCK * BLOCK
    y1 = min(H - 1, (int(rows[-1]) // BLOCK + 1) * BLOCK - 1)
    cols = np.flatnonzero(changed.any(axis=0))
    x0 = int(cols[0]) * 8 // BLOCK * BLOCK
    x1 = min(W - 1, ((int(cols[-1]) * 8) // BLOCK + 1) * BLOCK - 1)
    return int(x0), int(y0), int(x1), int(y1)


def crop(frame, x0, y0, x1, y1):
    """裁窗口数据, 窗口宽度按字节对齐."""
    x0 &= 0xFFF8
    x1 |= 7
    return frame[y0:y1 + 1, x0 // 8:(x1 + 1) // 8].tobytes()


async def find_device():
    for _ in range(6):
        res = await BleakScanner.discover(timeout=6, return_adv=True)
        for _, value in res.items():
            dev, _adv = value if isinstance(value, tuple) else (value, None)
            if "epd" in (dev.name or "").lower():
                return dev
        await asyncio.sleep(2)
    return None


class Panel:
    def __init__(self, client):
        self.c = client
        self.notes = []

    async def start(self):
        await self.c.start_notify(CMD, lambda _h, d: self.notes.append(bytes(d)))

    async def cmd(self, code, data=None):
        await self.c.write_gatt_char(CMD, pkt(0x03, [code]), response=True)
        if data is not None:
            await self.c.write_gatt_char(CMD, pkt(0x04, bytes(data)), response=True)

    async def init(self):
        """服务层 0x01, 复位面板并重建寄存器状态."""
        await self.c.write_gatt_char(CMD, pkt(0x01), response=True)

    async def window(self, x0, y0, x1, y1):
        x0 &= 0xFFF8
        x1 |= 7
        await self.cmd(0x90, [x0 >> 8, x0 & 255, x1 >> 8, x1 & 255,
                              y0 >> 8, y0 & 255, y1 >> 8, y1 & 255, 0x00])

    async def send(self, data):
        for i in range(0, len(data), 200):
            await self.c.write_gatt_char(CMD, pkt(0x04, data[i:i + 200]), response=True)
        return len(data)

    async def ask(self, code, prefix, timeout=3.0):
        self.notes.clear()
        await self.c.write_gatt_char(CMD, pkt(code), response=True)
        loops = int(timeout / 0.05)
        for _ in range(loops):
            for note in self.notes:
                if note.startswith(prefix):
                    return note.decode()
            await asyncio.sleep(0.05)
        return None

    async def refresh(self, timeout=40.0):
        self.notes.clear()
        await self.c.write_gatt_char(CMD, pkt(0x39), response=True)
        for _ in range(int(timeout / 0.05)):
            for note in self.notes:
                if note.startswith(b"ms="):
                    return int(note[3:])
            await asyncio.sleep(0.05)
        return -1

    async def lut(self, partial):
        await self.cmd(0x20, VCOM)
        await self.cmd(0x21, bytes(42) if partial else bytes(WW))
        await self.cmd(0x22, bytes(BW) + bytes(18))
        await self.cmd(0x23, bytes(WB) + bytes(18))
        await self.cmd(0x24, bytes(60) if partial else bytes(BB))
        await self.cmd(0x25, bytes(42))


async def push(frame_old, frame_new, force_full, clean=False):
    state_count = 0
    ready = False
    if STATE.exists():
        try:
            data = np.load(STATE)
            state_count = int(data["count"])
            ready = bool(data["ready"])
        except Exception:
            pass

    box = diff_box(frame_old, frame_new) if frame_old is not None else (0, 0, W - 1, H - 1)
    do_full = force_full or frame_old is None or not ready or state_count >= FULL_EVERY
    if not do_full and box is None:
        print("画面无变化, 跳过")
        return

    if do_full:
        box = (0, 0, W - 1, H - 1)
        state_count = 0

    print("%s 窗口 x=%d..%d y=%d..%d" % ("整屏" if do_full else "局刷", box[0], box[2], box[1], box[3]))

    dev = await find_device()
    if dev is None:
        print("找不到设备")
        sys.exit(2)
    client = BleakClient(dev, timeout=30)
    await client.connect()
    try:
        p = Panel(client)
        await p.start()

        state = await p.ask(0x3A, b"ready=")
        print("设备状态:", state)
        if state == "ready=0":
            do_full = True
            box = (0, 0, W - 1, H - 1)

        if do_full:
            await p.init()
            await asyncio.sleep(0.8)
            await p.cmd(0x00, [0x3F])
            await p.cmd(0x50, [0xA9, 0x07])
            await p.lut(partial=False)
            await p.window(0, 0, W - 1, H - 1)
            await p.cmd(0x13)
            sent = await p.send(frame_new.tobytes())
        else:
            await p.cmd(0x04)          # 上电, 不复位
            await asyncio.sleep(0.2)
            await p.lut(partial=not clean)
            await p.cmd(0x91)
            await p.window(*box)
            await p.cmd(0x13)
            sent = await p.send(crop(frame_new, *box))
            await p.cmd(0x92)

        ms = await p.refresh()
        print("刷新耗时 %d ms, 传输 %d 字节" % (ms, sent))
        np.savez(STATE, frame=frame_new, count=(0 if do_full else state_count + 1), ready=True)
    finally:
        await client.disconnect()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--clean", action="store_true", help="窗口内用完整波形, 不保留旧像素")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    new = load_frame(args.image)
    old = None
    if STATE.exists():
        try:
            old = np.load(STATE)["frame"]
            if old.shape != new.shape:
                old = None
        except Exception:
            old = None

    box = diff_box(old, new) if old is not None else (0, 0, W - 1, H - 1)
    print("差分框:", box)
    if args.dry_run:
        return
    asyncio.run(push(old, new, args.full, args.clean))


if __name__ == "__main__":
    main()

